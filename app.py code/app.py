import os
import sys
import logging
import json
from datetime import datetime
from flask import Flask, request, jsonify, redirect, url_for, render_template
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import check_password_hash
from config import Config
from functools import wraps
import ipaddress
from sqlalchemy import create_engine, Column, String, Integer, event, text
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError
import paho.mqtt.client as mqtt
from dash import Dash, dcc, html, dash_table, callback_context
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output, State, ALL
import sqlite3

# Initialize the Flask app
app = Flask(__name__)
app.config.from_object(Config)
CORS(app)

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Get users from config
users = Config.USERS

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    if user_id in users:
        return User(user_id)
    return None

# Function to get client IP
def get_client_ip():
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr

# Function to check if IP is local
def is_local_ip(ip):
    try:
        ip_addr = ipaddress.ip_address(ip)
        return ip_addr.is_private
    except ValueError:
        return False

# Custom decorator for local network bypass
def local_or_authenticated(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if is_local_ip(get_client_ip()):
            return f(*args, **kwargs)
        return login_required(f)(*args, **kwargs)
    return decorated_function

# Authentication routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if is_local_ip(get_client_ip()):
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if username in users and check_password_hash(users[username]['password_hash'], password):
            user = User(username)
            login_user(user)
            next_page = request.args.get('next')

            logger.debug(f"User '{username}' logged in. Redirecting to '{next_page or 'index'}'")

            # Security check for the next_page to prevent open redirects
            if next_page and next_page.startswith('/dashboard/'):
                return redirect(next_page)
            else:
                return redirect(url_for('index'))
        
        logger.debug("Invalid credentials provided.")
        return render_template('login.html', error='Invalid credentials')

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logger.debug(f"User '{current_user.id}' logged out.")
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@local_or_authenticated
def index():
    logger.debug("Redirecting to /dashboard/")
    return redirect('/dashboard/')

# Ensure the instance folder exists
instance_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
os.makedirs(instance_path, exist_ok=True)

# Database file path
db_path = os.path.join(instance_path, 'inventory.db')

# Create the SQLite engine and base for SQLAlchemy
engine = create_engine(
    f'sqlite:///{db_path}',
    connect_args={'check_same_thread': False},
    pool_size=5,
    pool_recycle=3600
)

# Set up SQLite PRAGMA for lock timeout and WAL mode
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout = 10000")  # 10 seconds timeout
    cursor.execute("PRAGMA journal_mode = WAL")    # Write-Ahead Logging to improve concurrency
    cursor.close()
    logger.debug("SQLite PRAGMA set for lock timeout and WAL mode.")

Base = declarative_base()

# Define the Inventory model (barcode, product name, and quantity)
class Inventory(Base):
    __tablename__ = 'inventory'
    id = Column(Integer, primary_key=True, autoincrement=True)
    barcode = Column(String, unique=True, nullable=False)
    product_name = Column(String)  # Product name mapped from barcode
    quantity = Column(Integer, default=1)

# Define the Purchase model to track purchases
class Purchase(Base):
    __tablename__ = 'purchase'
    id = Column(Integer, primary_key=True, autoincrement=True)
    customer = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    date_purchased = Column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

# Create the tables (if not exist)
Base.metadata.create_all(engine)
logger.debug("Database tables created (if not existing).")

# Create a scoped session
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Session = scoped_session(SessionFactory)

# Define the MQTT client
broker_url = "test.mosquitto.org"
mqtt_client = mqtt.Client()

# Connect to the MQTT broker
try:
    mqtt_client.connect(broker_url, 1883)  # Default port for MQTT
    mqtt_client.loop_start()  # Start the loop to process MQTT messages
    logger.debug("Connected to MQTT broker successfully.")
except Exception as e:
    logger.error(f"Failed to connect to MQTT broker: {e}")

# Function to publish messages to the MQTT broker
def publish_to_mqtt(action, data):
    message = {
        "action": action,
        "data": data
    }
    result = mqtt_client.publish("inventory/updates", json.dumps(message), qos=1)  # QoS 1 for delivery guarantee
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.error(f"Failed to publish MQTT message: {result.rc}")
    else:
        logger.debug(f"Published MQTT message: {message}")

# Updated barcode to product name mapping with new 6-digit prefixes
barcode_prefix_mapping = {
    '71013487523': 'Alex Silver',
    '71011154523': 'Newport Silver',
    '71011153523': 'Newport White and Pink',
    '71011156522': 'Newport Copper',
    '71011155523': 'Newport White and Gold',
    '71075750522': 'Newport Gunmetal',
    '71014181553': 'Newport Blue',
    '71011151522': 'Newport Ebony and Gold',
    '110481': 'Brighton Natural',
    '210477': 'Silver Rose',
    '210889': 'Classic Ebony Gold',
    '110649': '#430',
    '110128': 'Masterpiece',
    '410204': "In God's Care",
    '210923': 'Dartmouth Blue',
    '210654': 'Roseboro',
    '210921': 'Dartmouth Bronze',
    '210953': 'Kessens Bronze',
    '110411': 'Nordon Pine',
    '110664': '#435',
    '210937': 'Kessens Grey',
}

# Helper function to get inventory from the database
def get_inventory_from_db():
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT product_name, quantity FROM inventory")
        rows = cursor.fetchall()
        conn.close()
        logger.debug(f"Fetched inventory from DB: {rows}")
        return [{"product_name": row[0], "quantity": row[1]} for row in rows]
    except Exception as e:
        logger.error(f"Error fetching inventory from DB: {e}")
        return []

# Helper function to get recent purchases from the database
def get_recent_purchases_from_db():
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT customer, product_name, quantity, date_purchased
            FROM purchase
            WHERE date_purchased >= date('now', '-30 days')
            ORDER BY date_purchased DESC
        """)
        rows = cursor.fetchall()
        conn.close()
        logger.debug(f"Fetched recent purchases from DB: {rows}")
        return [{"customer": row[0], "product_name": row[1], "quantity": row[2], "date_purchased": row[3]} for row in rows]
    except Exception as e:
        logger.error(f"Error fetching recent purchases from DB: {e}")
        return []

# Helper function to get stock alerts from the database
def get_stock_alerts_from_db():
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT product_name, quantity FROM inventory WHERE quantity <= 2")
        rows = cursor.fetchall()
        conn.close()
        logger.debug(f"Fetched stock alerts from DB: {rows}")
        return [{"product_name": row[0], "quantity": row[1]} for row in rows]
    except Exception as e:
        logger.error(f"Error fetching stock alerts from DB: {e}")
        return []

# Flask before_request to enforce authentication on /dashboard/* routes
@app.before_request
def before_request_func():
    # Define the prefix for Dash app routes
    dash_prefix = '/dashboard/'

    # Check if the requested path starts with the Dash prefix
    if request.path.startswith(dash_prefix):
        # Determine if the client IP is local
        client_ip = get_client_ip()
        if not is_local_ip(client_ip):
            # If not local, check if the user is authenticated
            if not current_user.is_authenticated:
                # Redirect to the login page, preserving the original destination
                return redirect(url_for('login', next=request.url))
    # No action needed for non-Dash routes or local IPs
    return None

# Initialize Dash app with proper URL prefixes
dash_app = Dash(
    __name__,
    server=app,
    external_stylesheets=[dbc.themes.LITERA],
    suppress_callback_exceptions=True,  # Allows callbacks for dynamic components
    requests_pathname_prefix='/dashboard/',  # Handles incoming requests under /dashboard/
    routes_pathname_prefix='/dashboard/'     # Dash internal routing prefix
)

# Customer options for the dropdown menu
customer_options = [
    {'label': 'A.S. TURNER & SON FUNERAL HOME', 'value': 'A.S. TURNER & SON FUNERAL HOME'},
    {'label': 'ABBEY FUNERAL HOME', 'value': 'ABBEY FUNERAL HOME'},
    {'label': 'ADAMS FUNERAL HOME', 'value': 'ADAMS FUNERAL HOME'},
    {'label': 'AKINS-COBB FUNERALS&CREMATIONS', 'value': 'AKINS-COBB FUNERALS&CREMATIONS'},
    {'label': 'AL HALL FUNERAL DIRECTORS, INC.', 'value': 'AL HALL FUNERAL DIRECTORS, INC.'},
    {'label': 'ALABAMA HERITAGE FUNERAL HOME', 'value': 'ALABAMA HERITAGE FUNERAL HOME'},
    {'label': 'ALBRITTENS FUNERAL SERVICE', 'value': 'ALBRITTENS FUNERAL SERVICE'},
    {'label': 'ALBRITTON FUNERAL DIRECTORS', 'value': 'ALBRITTON FUNERAL DIRECTORS'},
    {'label': 'ALDRIDGE FUNERAL SERVICES', 'value': 'ALDRIDGE FUNERAL SERVICES'},
    {'label': 'ALLEN & ALLEN FUNERAL HOME', 'value': 'ALLEN & ALLEN FUNERAL HOME'},
    {'label': 'ALLEN FUNERAL HOME', 'value': 'ALLEN FUNERAL HOME'},
    {'label': 'ALLISON MEMORIAL CHAPEL', 'value': 'ALLISON MEMORIAL CHAPEL'},
    {'label': 'ANDERSON & MARSHALL FUNERAL HM', 'value': 'ANDERSON & MARSHALL FUNERAL HM'},
    {'label': 'ANGEL HEIGHTS FUNERAL HOME', 'value': 'ANGEL HEIGHTS FUNERAL HOME'},
    {'label': 'ARCHER FUNERAL HOME', 'value': 'ARCHER FUNERAL HOME'},
    {'label': "ARMOUR'S MEMORIAL FUNERAL HOME", 'value': "ARMOUR'S MEMORIAL FUNERAL HOME"},
    {'label': "ASHLEY'S JH WILLIAMS & SONS FH", 'value': "ASHLEY'S JH WILLIAMS & SONS FH"},
    {'label': 'BAKER FUNERAL HOME-', 'value': 'BAKER FUNERAL HOME-'},
    {'label': 'BAKER FUNERAL HOME.', 'value': 'BAKER FUNERAL HOME.'},
    {'label': 'BALDWIN FUNERAL HOME', 'value': 'BALDWIN FUNERAL HOME'},
    {'label': 'BANKS FUNERAL HOME', 'value': 'BANKS FUNERAL HOME'},
    {'label': 'BARNUM FUNERAL HOME', 'value': 'BARNUM FUNERAL HOME'},
    {'label': 'BATTLE & BATTLE FUNERAL HOME', 'value': 'BATTLE & BATTLE FUNERAL HOME'},
    {'label': 'BEGGS FUNERAL HOME', 'value': 'BEGGS FUNERAL HOME'},
    {'label': 'BEGGS FUNERAL HOME, INC.', 'value': 'BEGGS FUNERAL HOME, INC.'},
    {'label': 'BEGGS FUNERAL HOME, INC."', 'value': 'BEGGS FUNERAL HOME, INC."'},
    {'label': 'BENTLEY AND SONS FUNERAL HOME', 'value': 'BENTLEY AND SONS FUNERAL HOME'},
    {'label': 'BENTLEY CARSON MEMORIAL FH', 'value': 'BENTLEY CARSON MEMORIAL FH'},
    {'label': "BENTLEY'S & SON FUNERAL HOME", 'value': "BENTLEY'S & SON FUNERAL HOME"},
    {'label': 'BENTLEYS FUNERAL HOME', 'value': 'BENTLEYS FUNERAL HOME'},
    {'label': 'BEVIS FUNERAL HOME', 'value': 'BEVIS FUNERAL HOME'},
    {'label': 'BOONE FUNERAL HOME', 'value': 'BOONE FUNERAL HOME'},
    {'label': 'BOWEN-DONALDSON FH', 'value': 'BOWEN-DONALDSON FH'},
    {'label': 'BRADLEY ANDERSON FUNERAL HOME', 'value': 'BRADLEY ANDERSON FUNERAL HOME'},
    {'label': 'BRADWELL MORTUARY', 'value': 'BRADWELL MORTUARY'},
    {'label': 'BRANNEN FAMILY FUNERAL SERVICE', 'value': 'BRANNEN FAMILY FUNERAL SERVICE'},
    {'label': 'BRANNEN-NESMITH FUNERAL HOME', 'value': 'BRANNEN-NESMITH FUNERAL HOME'},
    {'label': 'BRIDGES FUNERAL HOME', 'value': 'BRIDGES FUNERAL HOME'},
    {'label': "BROCK'S HOMETOWN FUNERAL HOME", 'value': "BROCK'S HOMETOWN FUNERAL HOME"},
    {'label': 'BROOKSIDE FUNERAL HOME', 'value': 'BROOKSIDE FUNERAL HOME'},
    {'label': 'BRUTON MORTUARY', 'value': 'BRUTON MORTUARY'},
    {'label': 'BRYANT FUNERAL HOME', 'value': 'BRYANT FUNERAL HOME'},
    {'label': "BURDEN'S FUNERAL HOME", 'value': "BURDEN'S FUNERAL HOME"},
    {'label': 'BURTON FUNERAL HOME', 'value': 'BURTON FUNERAL HOME'},
    {'label': 'BYRD & FLANIGAN FUNERAL HOME', 'value': 'BYRD & FLANIGAN FUNERAL HOME'},
    {'label': 'C.O. HOLLOWAY MORTUARY', 'value': 'C.O. HOLLOWAY MORTUARY'},
    {'label': 'CARL WILLIAMS FUNERAL DIRECTORS', 'value': 'CARL WILLIAMS FUNERAL DIRECTORS'},
    {'label': 'CARSON McLANE FUNERAL HOME', 'value': 'CARSON McLANE FUNERAL HOME'},
    {'label': 'CARTER FUNERAL HOME', 'value': 'CARTER FUNERAL HOME'},
    {'label': 'CARTER FUNERAL HOME.', 'value': 'CARTER FUNERAL HOME.'},
    {'label': 'CARTER OGLETHORPE CHAPEL', 'value': 'CARTER OGLETHORPE CHAPEL'},
    {'label': 'CELEBRATION OF LIFE MEMORIAL', 'value': 'CELEBRATION OF LIFE MEMORIAL'},
    {'label': 'CENTRAL FUNERAL HOME', 'value': 'CENTRAL FUNERAL HOME'},
    {'label': "CHANDLER'S FUNERAL HOME", 'value': "CHANDLER'S FUNERAL HOME"},
    {'label': 'CHAPEL HILL MORTUARY', 'value': 'CHAPEL HILL MORTUARY'},
    {'label': 'CHAPMAN FUNERAL CHAPEL', 'value': 'CHAPMAN FUNERAL CHAPEL'},
    {'label': 'CHAPMAN FUNERAL HOME', 'value': 'CHAPMAN FUNERAL HOME'},
    {'label': 'CHARLES MCDOUGALD FUNERAL HOME', 'value': 'CHARLES MCDOUGALD FUNERAL HOME'},
    {'label': 'CHARLES McCLELLAN FUNERAL HOME', 'value': 'CHARLES McCLELLAN FUNERAL HOME'},
    {'label': 'CLARK FUNERAL HOME', 'value': 'CLARK FUNERAL HOME'},
    {'label': 'CLARK FUNERAL HOME-', 'value': 'CLARK FUNERAL HOME-'},
    {'label': 'CLARK MEMORIAL FUNERAL SERVICE', 'value': 'CLARK MEMORIAL FUNERAL SERVICE'},
    {'label': 'CLAUDE A. MCKIBBEN & SONS FH', 'value': 'CLAUDE A. MCKIBBEN & SONS FH'},
    {'label': 'CLAYTON MEMORIAL CHAPEL', 'value': 'CLAYTON MEMORIAL CHAPEL'},
    {'label': 'CLOUD FUNERAL HOME', 'value': 'CLOUD FUNERAL HOME'},
    {'label': 'CM BROWN FUNERAL HOME', 'value': 'CM BROWN FUNERAL HOME'},
    {'label': 'COBB FUNERAL CHAPEL', 'value': 'COBB FUNERAL CHAPEL'},
    {'label': 'COES FUNERAL HOME', 'value': 'COES FUNERAL HOME'},
    {'label': 'COGGINS FUNERAL HOME', 'value': 'COGGINS FUNERAL HOME'},
    {'label': 'COLLINS FUNERAL HOME', 'value': 'COLLINS FUNERAL HOME'},
    {'label': 'COLONIAL FUNERAL HOME', 'value': 'COLONIAL FUNERAL HOME'},
    {'label': 'COLQUITT FUNERAL HOME', 'value': 'COLQUITT FUNERAL HOME'},
    {'label': 'COMMUNITY FUNERAL HOME', 'value': 'COMMUNITY FUNERAL HOME'},
    {'label': 'CONNER-WESTBERRY FUNERAL HOME', 'value': 'CONNER-WESTBERRY FUNERAL HOME'},
    {'label': 'COX-IVEY FUNERAL HOME', 'value': 'COX-IVEY FUNERAL HOME'},
    {'label': 'CRAIG R. TREMBLE FUNERAL HOME-', 'value': 'CRAIG R. TREMBLE FUNERAL HOME-'},
    {'label': 'CRAWFORD & MOULTRY FH', 'value': 'CRAWFORD & MOULTRY FH'},
    {'label': 'CROSBY FUNERAL HOME-', 'value': 'CROSBY FUNERAL HOME-'},
    {'label': 'CURTIS FUNERAL HOME', 'value': 'CURTIS FUNERAL HOME'},
    {'label': 'DEAL FUNERAL DIRECTORS', 'value': 'DEAL FUNERAL DIRECTORS'},
    {'label': 'D.A.E. ENTERPRISE, LLC', 'value': 'D.A.E. ENTERPRISE, LLC'},
    {'label': 'DARRELL E WATKINS FUNERAL HOME', 'value': 'DARRELL E WATKINS FUNERAL HOME'},
    {'label': 'DAVIS FUNERAL HOME', 'value': 'DAVIS FUNERAL HOME'},
    {'label': 'DAVIS MEMORIAL MORTUARY', 'value': 'DAVIS MEMORIAL MORTUARY'},
    {'label': 'DILLARD FUNERAL HOME', 'value': 'DILLARD FUNERAL HOME'},
    {'label': 'DIVINE MORTUARY & CREMATIONS', 'value': 'DIVINE MORTUARY & CREMATIONS'},
    {'label': 'DONALD TRIMBLE MORTUARY, INC.', 'value': 'DONALD TRIMBLE MORTUARY, INC.'},
    {'label': 'DORCHESTER FUNERAL HOME', 'value': 'DORCHESTER FUNERAL HOME'},
    {'label': 'DUDLEY FUNERAL HOME', 'value': 'DUDLEY FUNERAL HOME'},
    {'label': 'E.T. HOSLEY MEMORIAL', 'value': 'E.T. HOSLEY MEMORIAL'},
    {'label': 'EDWARDS-SMALL MORTUARY', 'value': 'EDWARDS-SMALL MORTUARY'},
    {'label': 'ELLIOTT FUNERAL HOME', 'value': 'ELLIOTT FUNERAL HOME'},
    {'label': 'ELLIOTT PARHAM MORTUARY', 'value': 'ELLIOTT PARHAM MORTUARY'},
    {'label': 'ELLISON MEMORIAL FUNERAL HOME', 'value': 'ELLISON MEMORIAL FUNERAL HOME'},
    {'label': 'ERIC BROWN FUNERAL HOME', 'value': 'ERIC BROWN FUNERAL HOME'},
    {'label': 'EVANS-SKIPPER FUNERAL HOME', 'value': 'EVANS-SKIPPER FUNERAL HOME'},
    {'label': 'F.L. SIMS FUNERAL HOME', 'value': 'F.L. SIMS FUNERAL HOME'},
    {'label': 'FAITH FUNERAL HOME', 'value': 'FAITH FUNERAL HOME'},
    {'label': 'FAMILY FIRST FUNERAL CARE', 'value': 'FAMILY FIRST FUNERAL CARE'},
    {'label': 'FAMILY FUNERAL HOME', 'value': 'FAMILY FUNERAL HOME'},
    {'label': 'FERGUSON FUNERAL HOME', 'value': 'FERGUSON FUNERAL HOME'},
    {'label': 'FIELDS FUNERAL HOME', 'value': 'FIELDS FUNERAL HOME'},
    {'label': 'FLANDERS MORRISON FUNERAL HOME', 'value': 'FLANDERS MORRISON FUNERAL HOME'},
    {'label': 'FLANIGAN FUNERAL HOME', 'value': 'FLANIGAN FUNERAL HOME'},
    {'label': 'FORD-STEWART FUNERAL HOME', 'value': 'FORD-STEWART FUNERAL HOME'},
    {'label': 'FRAZIER AND SON FUNERAL HOME', 'value': 'FRAZIER AND SON FUNERAL HOME'},
    {'label': 'FREDERICK-DEAN FUNERAL HOME', 'value': 'FREDERICK-DEAN FUNERAL HOME'},
    {'label': 'FREEMAN FUNERAL HOME', 'value': 'FREEMAN FUNERAL HOME'},
    {'label': 'FUQUA - BANKSTON FUNERAL HOME', 'value': 'FUQUA - BANKSTON FUNERAL HOME'},
    {'label': 'GARDENS OF MEMORY-BAINBRIDGE', 'value': 'GARDENS OF MEMORY-BAINBRIDGE'},
    {'label': 'GATLIN MORTUARY INC.', 'value': 'GATLIN MORTUARY INC.'},
    {'label': 'GETHSEMANE MEMORIALS', 'value': 'GETHSEMANE MEMORIALS'},
    {'label': 'GLOVER MORTUARY', 'value': 'GLOVER MORTUARY'},
    {'label': 'GODFREY FUNERAL HOME, LLC', 'value': 'GODFREY FUNERAL HOME, LLC'},
    {'label': 'GOLDEN GATES BURIAL& CREMATION', 'value': 'GOLDEN GATES BURIAL& CREMATION'},
    {'label': 'GRACE FUNERAL & CREMATION SVCS', 'value': 'GRACE FUNERAL & CREMATION SVCS'},
    {'label': 'GREEN HILLS FUNERAL HOME', 'value': 'GREEN HILLS FUNERAL HOME'},
    {'label': 'GREG HANCOCK FUNERAL CHAPEL', 'value': 'GREG HANCOCK FUNERAL CHAPEL'},
    {'label': 'GREGORY B. LEVETT & SONS FH', 'value': 'GREGORY B. LEVETT & SONS FH'},
    {'label': 'GROOMS FUNERAL HOME', 'value': 'GROOMS FUNERAL HOME'},
    {'label': 'GRUBBS FUNERAL HOME', 'value': 'GRUBBS FUNERAL HOME'},
    {'label': 'GUERRY FUNERAL HOME', 'value': 'GUERRY FUNERAL HOME'},
    {'label': "GUS THORNHILL'S FUNERAL HOME", 'value': "GUS THORNHILL'S FUNERAL HOME"},
    {'label': "HADLEY'S FUNERAL HOME", 'value': "HADLEY'S FUNERAL HOME"},
    {'label': 'HAGAN FUNERAL SERVICE', 'value': 'HAGAN FUNERAL SERVICE'},
    {'label': 'HAILE FUNERAL HOME', 'value': 'HAILE FUNERAL HOME'},
    {'label': 'HAISTEN FUNERAL HOME', 'value': 'HAISTEN FUNERAL HOME'},
    {'label': 'HALL & HALL FUNERAL HOME', 'value': 'HALL & HALL FUNERAL HOME'},
    {'label': 'HALLS FUNERAL HOME', 'value': 'HALLS FUNERAL HOME'},
    {'label': 'HAMILTON-BURCH FH', 'value': 'HAMILTON-BURCH FH'},
    {'label': 'HAMMOND FUNERAL HOME', 'value': 'HAMMOND FUNERAL HOME'},
    {'label': 'HANCOCK FUNERAL HOME', 'value': 'HANCOCK FUNERAL HOME'},
    {'label': 'HARRELLS FUNERAL HOME', 'value': 'HARRELLS FUNERAL HOME'},
    {'label': 'HARRINGTON FAMILY FS- WAYCROSS', 'value': 'HARRINGTON FAMILY FS- WAYCROSS'},
    {'label': 'HARRINGTON FUNERAL HOME', 'value': 'HARRINGTON FUNERAL HOME'},
    {'label': 'HARRINGTON MORTUARY &CREMATION', 'value': 'HARRINGTON MORTUARY &CREMATION'},
    {'label': 'HARRIS MORTUARY, INC.', 'value': 'HARRIS MORTUARY, INC.'},
    {'label': 'HART FUNERAL HOME', 'value': 'HART FUNERAL HOME'},
    {'label': 'HARVEY FUNERAL HOME', 'value': 'HARVEY FUNERAL HOME'},
    {'label': 'HATCHER-PEOPLES FUNERAL HOME', 'value': 'HATCHER-PEOPLES FUNERAL HOME'},
    {'label': "HENDERSON'S MEMORIAL CHAPEL", 'value': "HENDERSON'S MEMORIAL CHAPEL"},
    {'label': 'HERITAGE FUNERAL HOME', 'value': 'HERITAGE FUNERAL HOME'},
    {'label': 'HERITAGE FUNERAL HOME-', 'value': 'HERITAGE FUNERAL HOME-'},
    {'label': 'HERSCHEL THORNTON MORTUARY', 'value': 'HERSCHEL THORNTON MORTUARY'},
    {'label': 'HICKS & SONS MORTUARY', 'value': 'HICKS & SONS MORTUARY'},
    {'label': 'HICKS FUNERAL HOME', 'value': 'HICKS FUNERAL HOME'},
    {'label': 'HIGGINS FUNERAL HOME', 'value': 'HIGGINS FUNERAL HOME'},
    {'label': 'HIGGS FUNERAL HOME', 'value': 'HIGGS FUNERAL HOME'},
    {'label': 'HILL-WATSON MEMORIAL CHAPEL', 'value': 'HILL-WATSON MEMORIAL CHAPEL'},
    {'label': 'HILL-WATSON-PEOPLES FUNERAL HM', 'value': 'HILL-WATSON-PEOPLES FUNERAL HM'},
    {'label': 'HILLS FUNERAL HOME', 'value': 'HILLS FUNERAL HOME'},
    {'label': 'HOLMAN FUNERAL HOME-OZARK', 'value': 'HOLMAN FUNERAL HOME-OZARK'},
    {'label': 'HOLMAN-HEADLAND MORTUARY. INC', 'value': 'HOLMAN-HEADLAND MORTUARY. INC'},
    {'label': 'HOPKINS MORTUARY', 'value': 'HOPKINS MORTUARY'},
    {'label': 'HOUSE OF TOWNS MORTUARY', 'value': 'HOUSE OF TOWNS MORTUARY'},
    {'label': 'HOWARD FUNERAL HOME', 'value': 'HOWARD FUNERAL HOME'},
    {'label': "HUFF'S INTERNATIONAL FH", 'value': "HUFF'S INTERNATIONAL FH"},
    {'label': 'HUNTER-ALLEN-MYHAND FH', 'value': 'HUNTER-ALLEN-MYHAND FH'},
    {'label': 'HUTCHESON-CROFT FUNERAL HOME', 'value': 'HUTCHESON-CROFT FUNERAL HOME'},
    {'label': "HUTCHESON'S MEMORIAL CHAPEL", 'value': "HUTCHESON'S MEMORIAL CHAPEL"},
    {'label': 'INDEPENDENT FUNERAL HOME', 'value': 'INDEPENDENT FUNERAL HOME'},
    {'label': 'IVEY FUNERAL HOME', 'value': 'IVEY FUNERAL HOME'},
    {'label': 'IVEY FUNERAL HOME', 'value': 'IVEY FUNERAL HOME'},
    {'label': 'IVIE FUNERAL HOME', 'value': 'IVIE FUNERAL HOME'},
    {'label': 'J. COLLINS FUNERAL HOME', 'value': 'J. COLLINS FUNERAL HOME'},
    {'label': 'J.L. LITMAN FUNERAL SERVICE', 'value': 'J.L. LITMAN FUNERAL SERVICE'},
    {'label': 'J.MELLIE NESMITH FH', 'value': 'J.MELLIE NESMITH FH'},
    {'label': 'J.W. WILLIAMS FUNERAL HOME', 'value': 'J.W. WILLIAMS FUNERAL HOME'},
    {'label': 'JAMES & LIPFORD FUNERAL HOME', 'value': 'JAMES & LIPFORD FUNERAL HOME'},
    {'label': 'JAMES & SIKES FUNERAL HOMES', 'value': 'JAMES & SIKES FUNERAL HOMES'},
    {'label': 'JAMES A. THOMAS F H', 'value': 'JAMES A. THOMAS F H'},
    {'label': 'JANAZA SERVICES OF GA INC.', 'value': 'JANAZA SERVICES OF GA INC.'},
    {'label': 'JEFF JONES FUNERAL HOME', 'value': 'JEFF JONES FUNERAL HOME'},
    {'label': 'JEFFCOAT - TRANT FUNERAL HOME', 'value': 'JEFFCOAT - TRANT FUNERAL HOME'},
    {'label': 'JEFFCOAT FUNERAL HOME', 'value': 'JEFFCOAT FUNERAL HOME'},
    {'label': 'JH WILLIAMS AND SONS INC.', 'value': 'JH WILLIAMS AND SONS INC.'},
    {'label': 'JOHNSON & SON FUNERAL SERVICE', 'value': 'JOHNSON & SON FUNERAL SERVICE'},
    {'label': 'JOHNSON BROWN SERVICE FH', 'value': 'JOHNSON BROWN SERVICE FH'},
    {'label': 'JOHNSON FUNERAL & CREMATION', 'value': 'JOHNSON FUNERAL & CREMATION'},
    {'label': 'JOINER-ANDERSON FUNERAL HOME', 'value': 'JOINER-ANDERSON FUNERAL HOME'},
    {'label': 'JONES BROTHERS MEMORIAL CHAPEL', 'value': 'JONES BROTHERS MEMORIAL CHAPEL'},
    {'label': 'JORDAN FUNERAL HOME', 'value': 'JORDAN FUNERAL HOME'},
    {'label': 'JOSEPH W. JONES FUNERAL HOME', 'value': 'JOSEPH W. JONES FUNERAL HOME'},
    {'label': 'JP MOORE MORTUARY & CREMATION', 'value': 'JP MOORE MORTUARY & CREMATION'},
    {'label': 'K.L. CLOSE FUNERAL HOME', 'value': 'K.L. CLOSE FUNERAL HOME'},
    {'label': 'KIMBRELL-STERN FD', 'value': 'KIMBRELL-STERN FD'},
    {'label': 'KIMBROUGH FUNERAL HOME', 'value': 'KIMBROUGH FUNERAL HOME'},
    {'label': 'KING BROTHERS FUNERAL HOME', 'value': 'KING BROTHERS FUNERAL HOME'},
    {'label': 'KURT DEAL FUNERAL', 'value': 'KURT DEAL FUNERAL'},
    {'label': 'LAKES-DUNSON-ROBERTSON FH', 'value': 'LAKES-DUNSON-ROBERTSON FH'},
    {'label': 'LAKEVIEW MEMORY GARDENS', 'value': 'LAKEVIEW MEMORY GARDENS'},
    {'label': "LAMB'S INTERNATIONAL FH", 'value': "LAMB'S INTERNATIONAL FH"},
    {'label': 'LANE MEMORIAL CHAPEL', 'value': 'LANE MEMORIAL CHAPEL'},
    {'label': 'LEAK-MEMORY FH/ LOC 4338', 'value': 'LEAK-MEMORY FH/ LOC 4338'},
    {'label': 'LEES FUNERAL HOME & CREMATORY', 'value': 'LEES FUNERAL HOME & CREMATORY'},
    {'label': 'LEMON FUNERAL HOME', 'value': 'LEMON FUNERAL HOME'},
    {'label': 'LEONARD FUNERAL HOME', 'value': 'LEONARD FUNERAL HOME'},
    {'label': 'LESTER LACKEY AND SONS FH', 'value': 'LESTER LACKEY AND SONS FH'},
    {'label': 'LEWIS MORTUARY', 'value': 'LEWIS MORTUARY'},
    {'label': 'LIFESONG FUNERAL HOME', 'value': 'LIFESONG FUNERAL HOME'},
    {'label': 'LINVILLE MEMORIAL FUNERAL HOME', 'value': 'LINVILLE MEMORIAL FUNERAL HOME'},
    {'label': 'LITTLE-WARD FUNERAL HOME', 'value': 'LITTLE-WARD FUNERAL HOME'},
    {'label': 'LOVEIN FUNERAL HOME', 'value': 'LOVEIN FUNERAL HOME'},
    {'label': 'LOWE FUNERAL HOME', 'value': 'LOWE FUNERAL HOME'},
    {'label': 'LUKE STRONG & SON MORTUARY', 'value': 'LUKE STRONG & SON MORTUARY'},
    {'label': 'LUNSFORD FUNERAL HOME', 'value': 'LUNSFORD FUNERAL HOME'},
    {'label': 'M.D. WALKER FUNERAL HOME', 'value': 'M.D. WALKER FUNERAL HOME'},
    {'label': 'MACKY WILSON JENNINGS FH', 'value': 'MACKY WILSON JENNINGS FH'},
    {'label': 'MAGNOLIA CREMATIONS', 'value': 'MAGNOLIA CREMATIONS'},
    {'label': 'MANRY JORDAN HODGES FH', 'value': 'MANRY JORDAN HODGES FH'},
    {'label': 'MARIANNA CHAPEL FUNERAL HOME', 'value': 'MARIANNA CHAPEL FUNERAL HOME'},
    {'label': 'MARIETTA FUNERAL HOME', 'value': 'MARIETTA FUNERAL HOME'},
    {'label': 'MARTIN LUTHER KING MEMORIAL', 'value': 'MARTIN LUTHER KING MEMORIAL'},
    {'label': 'MATHEWS FUNERAL HOME', 'value': 'MATHEWS FUNERAL HOME'},
    {'label': 'MAX BRANNON & SONS FH', 'value': 'MAX BRANNON & SONS FH'},
    {'label': 'MAY & SMITH FUNERAL DIRECTORS', 'value': 'MAY & SMITH FUNERAL DIRECTORS'},
    {'label': 'MCALPIN FUNERAL HOME', 'value': 'MCALPIN FUNERAL HOME'},
    {'label': 'MCCOY FUNERAL HOME-MANCHESTER', 'value': 'MCCOY FUNERAL HOME-MANCHESTER'},
    {'label': 'MCCULLOUGH FUNERAL HOME', 'value': 'MCCULLOUGH FUNERAL HOME'},
    {'label': "McIVER FUNERAL HOME", 'value': "McIVER FUNERAL HOME"},
    {'label': "MCKENZIE'S FUNERAL HOME", 'value': "MCKENZIE'S FUNERAL HOME"},
    {'label': 'MCKOON FUNERAL HOME', 'value': 'MCKOON FUNERAL HOME'},
    {'label': 'MCMULLEN FUNERAL HOME', 'value': 'MCMULLEN FUNERAL HOME'},
    {'label': 'MEADOWS FUNERAL HOME', 'value': 'MEADOWS FUNERAL HOME'},
    {'label': 'MEADOWS FUNERAL HOME, INC.', 'value': 'MEADOWS FUNERAL HOME, INC.'},
    {'label': 'MEMORY CHAPEL FUNERAL HOME', 'value': 'MEMORY CHAPEL FUNERAL HOME'},
    {'label': 'MILES FUNERAL HOME', 'value': 'MILES FUNERAL HOME'},
    {'label': 'MILES-ODUM FUNERAL HOME', 'value': 'MILES-ODUM FUNERAL HOME'},
    {'label': 'MILLER FUNERAL HOME TALLAPOOSA', 'value': 'MILLER FUNERAL HOME TALLAPOOSA'},
    {'label': 'MONROE COUNTY MEMORIAL CHAPEL', 'value': 'MONROE COUNTY MEMORIAL CHAPEL'},
    {'label': 'MOODY-DANIEL FUNERAL HOME', 'value': 'MOODY-DANIEL FUNERAL HOME'},
    {'label': 'MOORE FUNERAL HOME', 'value': 'MOORE FUNERAL HOME'},
    {'label': "MORGAN & SON'S FUNERAL HOME", 'value': "MORGAN & SON'S FUNERAL HOME"},
    {'label': 'MORGAN & SONS FUNERAL HOME', 'value': 'MORGAN & SONS FUNERAL HOME'},
    {'label': 'MUSIC FUNERAL HOME', 'value': 'MUSIC FUNERAL HOME'},
    {'label': 'MUSIC FUNERAL HOME -', 'value': 'MUSIC FUNERAL HOME -'},
    {'label': "NELSON'S MEMORIAL MORTUARY", 'value': "NELSON'S MEMORIAL MORTUARY"},
    {'label': 'NEW GENERATION MEMORIAL MORT.', 'value': 'NEW GENERATION MEMORIAL MORT.'},
    {'label': 'NOBLES FUNERAL HOME & CREMATORY', 'value': 'NOBLES FUNERAL HOME & CREMATORY'},
    {'label': 'OGLETHORPE FUNERAL CHAPEL', 'value': 'OGLETHORPE FUNERAL CHAPEL'},
    {'label': 'OXLEY-HEARD FUNERAL DIRECTORS', 'value': 'OXLEY-HEARD FUNERAL DIRECTORS'},
    {'label': 'PARKER - BRAMLETT FUNERAL HOME', 'value': 'PARKER - BRAMLETT FUNERAL HOME'},
    {'label': 'PARROTT FUNERAL HOME', 'value': 'PARROTT FUNERAL HOME'},
    {'label': 'PASCHAL MEMORIAL FUNERAL HOME', 'value': 'PASCHAL MEMORIAL FUNERAL HOME'},
    {'label': 'PASCO GAINER SR. FUNERAL HOME', 'value': 'PASCO GAINER SR. FUNERAL HOME'},
    {'label': 'PAULK FUNERAL HOME', 'value': 'PAULK FUNERAL HOME'},
    {'label': 'PEARSON - DIAL FUNERAL HOME', 'value': 'PEARSON - DIAL FUNERAL HOME'},
    {'label': "PEEL' FUNERAL HOME", 'value': "PEEL' FUNERAL HOME"},
    {'label': 'PEOPLES FUNERAL HOME', 'value': 'PEOPLES FUNERAL HOME'},
    {'label': "PEOPLES' FUNERAL HOME- T", 'value': "PEOPLES' FUNERAL HOME- T"},
    {'label': 'PERKINS FUNERAL HOME', 'value': 'PERKINS FUNERAL HOME'},
    {'label': 'PERRY BROTHERS FUNERAL HOME', 'value': 'PERRY BROTHERS FUNERAL HOME'},
    {'label': 'PERRY FUNERAL CHAPEL', 'value': 'PERRY FUNERAL CHAPEL'},
    {'label': 'PETERSON & WILLIAMS FH', 'value': 'PETERSON & WILLIAMS FH'},
    {'label': "PETERSON'S FUNERAL HOME", 'value': "PETERSON'S FUNERAL HOME"},
    {'label': 'PHILLIPS & RILEY FUNERAL HOME', 'value': 'PHILLIPS & RILEY FUNERAL HOME'},
    {'label': 'POOLE FUNERAL HOME & CREMATION', 'value': 'POOLE FUNERAL HOME & CREMATION'},
    {'label': 'PROGRESSIVE FUNERAL HOME', 'value': 'PROGRESSIVE FUNERAL HOME'},
    {'label': 'PROMISE LAND FUNERAL HOME', 'value': 'PROMISE LAND FUNERAL HOME'},
    {'label': 'RADNEY FUNERAL HOME', 'value': 'RADNEY FUNERAL HOME'},
    {'label': 'RAINEY FUNERAL HOME', 'value': 'RAINEY FUNERAL HOME'},
    {'label': 'RAINGE MEMORIAL CHAPEL', 'value': 'RAINGE MEMORIAL CHAPEL'},
    {'label': 'RAINWATER FUNERAL HOME', 'value': 'RAINWATER FUNERAL HOME'},
    {'label': 'REECE FUNERAL HOME', 'value': 'REECE FUNERAL HOME'},
    {'label': 'RELIHAN FUNERAL HOME', 'value': 'RELIHAN FUNERAL HOME'},
    {'label': 'RICHARDSON FUNERAL HOME', 'value': 'RICHARDSON FUNERAL HOME'},
    {'label': "RICHARDSON'S FAMILY FUNERAL CA", 'value': "RICHARDSON'S FAMILY FUNERAL CA"},
    {'label': 'RICHMOND HILL FUNERAL HOME', 'value': 'RICHMOND HILL FUNERAL HOME'},
    {'label': 'RICKETSON FUNERAL HOME', 'value': 'RICKETSON FUNERAL HOME'},
    {'label': "RIDOUT'S PRATTVILLE CHAPEL", 'value': "RIDOUT'S PRATTVILLE CHAPEL"},
    {'label': 'RINEHART & SONS FUNERAL HOME', 'value': 'RINEHART & SONS FUNERAL HOME'},
    {'label': 'ROLLINS FUNERAL HOME', 'value': 'ROLLINS FUNERAL HOME'},
    {'label': 'RONNIE L. STEWART FS', 'value': 'RONNIE L. STEWART FS'},
    {'label': 'ROOKS FUNERAL HOME', 'value': 'ROOKS FUNERAL HOME'},
    {'label': 'ROSADALE FUNERAL PARLOR, INC.', 'value': 'ROSADALE FUNERAL PARLOR, INC.'},
    {'label': 'ROSCOE JENKINS FUNERAL HOME', 'value': 'ROSCOE JENKINS FUNERAL HOME'},
    {'label': 'ROSS - CLAYTON FUNERAL HOME', 'value': 'ROSS - CLAYTON FUNERAL HOME'},
    {'label': 'ROYAL FUNERAL HOME', 'value': 'ROYAL FUNERAL HOME'},
    {'label': 'RUSSELL WRIGHT MORTUARY', 'value': 'RUSSELL WRIGHT MORTUARY'},
    {'label': 'SAMMONS FUNERAL HOME', 'value': 'SAMMONS FUNERAL HOME'},
    {'label': 'SCONIERS FUNERAL HOME', 'value': 'SCONIERS FUNERAL HOME'},
    {'label': 'SCOTT & ROBERTS FUNERAL HOME', 'value': 'SCOTT & ROBERTS FUNERAL HOME'},
    {'label': 'SELMA FUNERAL HOME', 'value': 'SELMA FUNERAL HOME'},
    {'label': 'SERENITY FUNERAL HOME', 'value': 'SERENITY FUNERAL HOME'},
    {'label': 'SEROYER FUNERAL HOME', 'value': 'SEROYER FUNERAL HOME'},
    {'label': 'SHEPARD- ROBERSON FUNERAL HOME', 'value': 'SHEPARD- ROBERSON FUNERAL HOME'},
    {'label': 'SHERRELL-WESTBERRY FUNERAL HOM', 'value': 'SHERRELL-WESTBERRY FUNERAL HOM'},
    {'label': "SHIPP'S FUNERAL HOME", 'value': "SHIPP'S FUNERAL HOME"},
    {'label': 'SIMS FUNERAL HOME', 'value': 'SIMS FUNERAL HOME'},
    {'label': 'SIMS FUNERAL HOME -', 'value': 'SIMS FUNERAL HOME -'},
    {'label': 'SMITH FUNERAL HOME', 'value': 'SMITH FUNERAL HOME'},
    {'label': 'SO. CREMATIONS AT HOLLY HILL', 'value': 'SO. CREMATIONS AT HOLLY HILL'},
    {'label': 'SONJA COAXUM', 'value': 'SONJA COAXUM'},
    {'label': 'SOUTHERN HERITAGE FUNERAL HOME', 'value': 'SOUTHERN HERITAGE FUNERAL HOME'},
    {'label': 'SOUTHERN MEMORIAL FH', 'value': 'SOUTHERN MEMORIAL FH'},
    {'label': 'SOUTHVIEW MORTUARY', 'value': 'SOUTHVIEW MORTUARY'},
    {'label': 'SPAULDING & BARNES FH', 'value': 'SPAULDING & BARNES FH'},
    {'label': 'STANFORD MEMORIAL CHAPEL', 'value': 'STANFORD MEMORIAL CHAPEL'},
    {'label': 'STANLEY FUNERAL HOME', 'value': 'STANLEY FUNERAL HOME'},
    {'label': 'STEVENS FUNERAL HOME', 'value': 'STEVENS FUNERAL HOME'},
    {'label': 'STEVENS-MCGHEE FUNERAL HOME', 'value': 'STEVENS-MCGHEE FUNERAL HOME'},
    {'label': 'STOKES - SOUTHERLAND F.H.', 'value': 'STOKES - SOUTHERLAND F.H.'},
    {'label': 'STOVALL FUNERAL HOME', 'value': 'STOVALL FUNERAL HOME'},
    {'label': 'STRIFFLER - HAMBY MORTUARY', 'value': 'STRIFFLER - HAMBY MORTUARY'},
    {'label': 'STRIFFLER-HAMBY MORTUARY', 'value': 'STRIFFLER-HAMBY MORTUARY'},
    {'label': 'STRONG & JONES FUNERAL HOME', 'value': 'STRONG & JONES FUNERAL HOME'},
    {'label': 'SUNSET MEMORIAL PARK', 'value': 'SUNSET MEMORIAL PARK'},
    {'label': "SWAIN'S FUNERAL HOME", 'value': "SWAIN'S FUNERAL HOME"},
    {'label': 'T.J. BEGGS JR. & SONS FH', 'value': 'T.J. BEGGS JR. & SONS FH'},
    {'label': 'T.V.WILLIAMS FUNERAL HOME', 'value': 'T.V.WILLIAMS FUNERAL HOME'},
    {'label': 'TAYLOR FUNERAL HOME', 'value': 'TAYLOR FUNERAL HOME'},
    {'label': 'TERRY FAMILY FUNERAL HOME', 'value': 'TERRY FAMILY FUNERAL HOME'},
    {'label': 'TERRY FAMILY-TALBOTTON CHAPEL', 'value': 'TERRY FAMILY-TALBOTTON CHAPEL'},
    {'label': 'THE PROMISE LAND FUNERAL HOME', 'value': 'THE PROMISE LAND FUNERAL HOME'},
    {'label': 'THOMAS & SON HOME FOR FUNERALS', 'value': 'THOMAS & SON HOME FOR FUNERALS'},
    {'label': 'THOMAS C. STRICKLAND & SONS FH', 'value': 'THOMAS C. STRICKLAND & SONS FH'},
    {'label': 'THOMAS MEMORIAL F H.', 'value': 'THOMAS MEMORIAL F H.'},
    {'label': 'THOMAS SCROGGS FUNERAL HOME', 'value': 'THOMAS SCROGGS FUNERAL HOME'},
    {'label': 'THOMPSON-STRICKLAND- WATERS FH', 'value': 'THOMPSON-STRICKLAND- WATERS FH'},
    {'label': 'THORNTON FUNERAL HOME', 'value': 'THORNTON FUNERAL HOME'},
    {'label': 'TOWNS FUNERAL HOME', 'value': 'TOWNS FUNERAL HOME'},
    {'label': 'TOWNSEND BROTHERS FUNERAL HOME', 'value': 'TOWNSEND BROTHERS FUNERAL HOME'},
    {'label': 'TRINITY FUNERAL HOME', 'value': 'TRINITY FUNERAL HOME'},
    {'label': 'UNITY FUNERAL HOME', 'value': 'UNITY FUNERAL HOME'},
    {'label': 'UNITY FUNERAL HOME', 'value': 'UNITY FUNERAL HOME'},
    {'label': 'VANCE-BROOKS - COLUMBUS', 'value': 'VANCE-BROOKS - COLUMBUS'},
    {'label': 'VANCE-BROOKS - PHENIX CITY', 'value': 'VANCE-BROOKS - PHENIX CITY'},
    {'label': 'VANN FUNERAL HOME', 'value': 'VANN FUNERAL HOME'},
    {'label': 'VIDALIA FUNERAL HOME', 'value': 'VIDALIA FUNERAL HOME'},
    {'label': 'VINCENT R. DRUMMER FH', 'value': 'VINCENT R. DRUMMER FH'},
    {'label': 'VINES FUNERAL HOME', 'value': 'VINES FUNERAL HOME'},
    {'label': 'W.D. LEMON & SONS FUNERAL HOME', 'value': 'W.D. LEMON & SONS FUNERAL HOME'},
    {'label': 'WAINWRIGHT & PARLOR FUNERAL FH', 'value': 'WAINWRIGHT & PARLOR FUNERAL FH'},
    {'label': "WARD'S FUNERAL HOME", 'value': "WARD'S FUNERAL HOME"},
    {'label': 'WARREN FUNERAL SERVICES', 'value': 'WARREN FUNERAL SERVICES'},
    {'label': 'WATKINS FUNERAL HOME INC.', 'value': 'WATKINS FUNERAL HOME INC.'},
    {'label': 'WATKINS FUNERAL HOME MCDONOUGH', 'value': 'WATKINS FUNERAL HOME MCDONOUGH'},
    {'label': 'WATKINS MORTUARY, INC.', 'value': 'WATKINS MORTUARY, INC.'},
    {'label': 'WATSON-HUNT FUNERAL HOME', 'value': 'WATSON-HUNT FUNERAL HOME'},
    {'label': 'WATSON-MATHEWS FUNERAL HOME', 'value': 'WATSON-MATHEWS FUNERAL HOME'},
    {'label': 'WAY - WATSON FUNERAL HOME', 'value': 'WAY - WATSON FUNERAL HOME'},
    {'label': 'WAY- WATSON FUNERAL HOME-BV', 'value': 'WAY- WATSON FUNERAL HOME-BV'},
    {'label': 'WELCH & BRINKLEY MORTUARY', 'value': 'WELCH & BRINKLEY MORTUARY'},
    {'label': 'WEST COBB FUNERAL HOME', 'value': 'WEST COBB FUNERAL HOME'},
    {'label': 'WEST MORTUARY, INC.- A', 'value': 'WEST MORTUARY, INC.- A'},
    {'label': "WEST'S MORTUARY - M", 'value': "WEST'S MORTUARY - M"},
    {'label': 'WESTON FUNERAL HOME', 'value': 'WESTON FUNERAL HOME'},
    {'label': 'WHIDDON-SHIVER FUNERAL HOME', 'value': 'WHIDDON-SHIVER FUNERAL HOME'},
    {'label': 'WHITE CHAPEL FUNERAL HOME', 'value': 'WHITE CHAPEL FUNERAL HOME'},
    {'label': 'WHITE FUNERAL & CREMATIONS', 'value': 'WHITE FUNERAL & CREMATIONS'},
    {'label': 'WILLIAMS FUNERAL HOME', 'value': 'WILLIAMS FUNERAL HOME'},
    {'label': 'WILLIAMS FUNERAL HOME - GRACE.', 'value': 'WILLIAMS FUNERAL HOME - GRACE.'},
    {'label': 'WILLIAMS MORTUARY', 'value': 'WILLIAMS MORTUARY'},
    {'label': 'WILLIAMS-WESTBERRY FUNERAL HOM', 'value': 'WILLIAMS-WESTBERRY FUNERAL HOM'},
    {'label': 'WILLIE A WATKINS FH - CAROL', 'value': 'WILLIE A WATKINS FH - CAROL'},
    {'label': 'WILLIE A WATKINS FH- RIVERDALE', 'value': 'WILLIE A WATKINS FH- RIVERDALE'},
    {'label': 'WILLIE WATKINS F.H.-LITHONIA', 'value': 'WILLIE WATKINS F.H.-LITHONIA'},
    {'label': 'WILLIE WATKINS FH - DOUG', 'value': 'WILLIE WATKINS FH - DOUG'},
    {'label': "WILLIFORD'S FUNERAL HOME", 'value': "WILLIFORD'S FUNERAL HOME"},
    {'label': 'WILLIS-JAMERSON-BRASWELL FH', 'value': 'WILLIS-JAMERSON-BRASWELL FH'},
    {'label': 'WILSON FUNERAL HOME', 'value': 'WILSON FUNERAL HOME'},
    {'label': 'WIMBERLY FUNERAL HOME', 'value': 'WIMBERLY FUNERAL HOME'},
    {'label': 'WINNS FUNERAL HOME', 'value': 'WINNS FUNERAL HOME'}
]
# Application layout with navigation
dash_app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    dbc.NavbarSimple(
        children=[
            dbc.NavItem(dbc.NavLink("Home", href="/dashboard/")),
            dbc.NavItem(dbc.NavLink("Orders", href="/dashboard/orders")),
            dbc.NavItem(dbc.NavLink("Recent Purchases", href="/dashboard/recent-purchases")),
            dbc.NavItem(dbc.NavLink("Stock Alerts", href="/dashboard/stock-alerts")),
        ],
        brand="Service Casket Dashboard",
        brand_href="/dashboard/",
        color="darkblue",
        dark=True,
    ),
    html.Div(id='page-content'),
    # Removed 'inventory-update-output' as it's no longer needed
    html.Div(id='print-output', style={'display': 'none'}),
])

# Home Page Layout
def home_layout():
    inventory = get_inventory_from_db()
    product_names = sorted(set(item['product_name'] for item in inventory))
    product_options = [{'label': name, 'value': name} for name in product_names]

    return dbc.Container([
        # Search bar for products (dropdown with autocomplete)
        dbc.Row([
            dbc.Col([
                html.Label("Search by Product Name"),
                dcc.Dropdown(
                    id='inventory-search',
                    options=product_options,
                    placeholder='Type to search...',
                    clearable=True,
                    searchable=True,
                    style={'width': '100%'}
                ),
            ], width=6),
        ], justify="start", style={'marginTop': '20px'}),

        # Inventory DataTable with "Add Quantity" column
        dbc.Row([
            dbc.Col([
                dash_table.DataTable(
                    id='inventory-table',
                    columns=[
                        {"name": "Product Name", "id": "product_name"},
                        {"name": "Quantity", "id": "quantity"},
                        {"name": "Add Quantity", "id": "add_quantity", "type": 'numeric', "editable": True},
                    ],
                    data=[{**item, 'add_quantity': ''} for item in inventory],  # Initialize "Add Quantity" as empty
                    style_data_conditional=[
                        {
                            'if': {
                                'filter_query': '{quantity} <= 2',
                                'column_id': 'quantity'
                            },
                            'backgroundColor': 'tomato',
                            'color': 'white',
                        },
                    ],
                    editable=True,
                    style_cell={'textAlign': 'left'},
                    style_table={'width': '100%'},
                    style_cell_conditional=[
                        {'if': {'column_id': 'product_name'}, 'width': '50%'},
                        {'if': {'column_id': 'quantity'}, 'width': '25%'},
                        {'if': {'column_id': 'add_quantity'}, 'width': '25%'},
                    ],
                )
            ], width=8),
        ], justify="start", style={'marginTop': '20px'}),
    ], fluid=True)

# Orders Page Layout
def orders_layout():
    inventory = get_inventory_from_db()
    casket_options = [{'label': item['product_name'], 'value': item['product_name']} for item in inventory]

    return dbc.Container([
        dbc.Row([
            dbc.Col(html.H2("Create New Order"), width=12)
        ], justify="start", style={'marginTop': '20px'}),

        # Customer selection
        dbc.Row([
            dbc.Col([
                html.Label("Select Customer"),
                dcc.Dropdown(
                    id='customer-dropdown',
                    options=customer_options,
                    placeholder="Select a customer",
                    style={'width': '100%'}
                ),
            ], width=4),
        ], justify="start", style={'marginTop': '20px'}),

        # Order Items
        html.Div(id='order-items', children=[
            create_order_item(0, casket_options)
        ]),

        # Add another item button
        dbc.Row([
            dbc.Col([
                html.Button("Add Another Item", id='add-item-button', n_clicks=0, className='btn btn-secondary'),
            ], width=4),
        ], justify="start", style={'marginTop': '20px'}),

        # Confirm and Generate Order Summary buttons
        dbc.Row([
            dbc.Col([
                html.Button("Confirm Order", id='confirm-order-button', n_clicks=0, className='btn btn-success'),
                html.Button("Generate Order Summary", id='generate-order-button', n_clicks=0, className='btn btn-info', style={'marginLeft': '10px'}),
            ], width=6),
        ], justify="start", style={'marginTop': '20px'}),

        # Order confirmation message and order summary
        dbc.Row([
            dbc.Col([
                html.Div(id='order-confirmation', style={'marginTop': '20px'}),
                html.Div(id='order-summary', style={'marginTop': '20px'}),
            ], width=12),
        ], justify="start"),
    ], fluid=True)

def create_order_item(index, casket_options):
    return html.Div([
        dbc.Row([
            dbc.Col([
                html.Label(f"Select Casket"),
                dcc.Dropdown(
                    id={'type': 'casket-dropdown', 'index': index},
                    options=casket_options,
                    placeholder="Select a casket",
                    style={'width': '100%'},
                    clearable=True,
                    searchable=True,
                ),
            ], width=4),
            dbc.Col([
                html.Label("Quantity"),
                dcc.Input(
                    id={'type': 'quantity-input', 'index': index},
                    type='number',
                    min=1,
                    placeholder='Enter quantity',
                    style={'width': '100%'}
                ),
            ], width=2),
        ], justify="start", style={'marginTop': '10px'}),
    ], id={'type': 'order-item', 'index': index})

# Recent Purchases Page Layout
def recent_purchases_layout():
    recent_purchases = get_recent_purchases_from_db()
    # Get unique customer names and product names for filters
    customer_names = sorted(set(item['customer'] for item in recent_purchases))
    product_names = sorted(set(item['product_name'] for item in recent_purchases))

    customer_options = [{'label': name, 'value': name} for name in customer_names]
    product_options = [{'label': name, 'value': name} for name in product_names]

    return dbc.Container([
        dbc.Row([
            dbc.Col(html.H2("Recent Purchases"), width=12)
        ], justify="start", style={'marginTop': '20px'}),

        # Search filters for customer and product name (dropdowns with autocomplete)
        dbc.Row([
            dbc.Col([
                html.Label("Filter by Customer"),
                dcc.Dropdown(
                    id='customer-filter',
                    options=customer_options,
                    placeholder='Select customers...',
                    clearable=True,
                    searchable=True,
                    multi=True,
                    style={'width': '100%'}
                ),
            ], width=4),
            dbc.Col([
                html.Label("Filter by Product Name"),
                dcc.Dropdown(
                    id='product-filter',
                    options=product_options,
                    placeholder='Select products...',
                    clearable=True,
                    searchable=True,
                    multi=True,
                    style={'width': '100%'}
                ),
            ], width=4),
        ], justify="start", style={'marginTop': '20px'}),

        dbc.Row([
            dbc.Col([
                dash_table.DataTable(
                    id='recent-purchases-table',
                    columns=[
                        {"name": "Customer", "id": "customer"},
                        {"name": "Product Name", "id": "product_name"},
                        {"name": "Quantity", "id": "quantity"},
                        {"name": "Date Purchased", "id": "date_purchased"},
                    ],
                    data=recent_purchases,
                    style_cell={'textAlign': 'left'},
                    style_table={'width': '100%'},
                    style_data_conditional=[{
                        'if': {
                            'row_index': 'odd'
                        },
                        'backgroundColor': 'rgb(248, 248, 248)'
                    }],
                    style_as_list_view=True,
                )
            ], width=12),
        ], justify="start", style={'marginTop': '20px'}),
    ], fluid=True)

# Stock Alerts Page Layout
def stock_alerts_layout():
    stock_alerts = get_stock_alerts_from_db()

    return dbc.Container([
        dbc.Row([
            dbc.Col(html.H2("Stock Alerts"), width=12)
        ], justify="start", style={'marginTop': '20px'}),

        dbc.Row([
            dbc.Col([
                dash_table.DataTable(
                    id='stock-alerts-table',
                    columns=[
                        {"name": "Product Name", "id": "product_name"},
                        {"name": "Quantity", "id": "quantity"},
                    ],
                    data=stock_alerts,
                    style_data_conditional=[
                        {
                            'if': {
                                'filter_query': '{quantity} <= 2',
                                'column_id': 'quantity'
                            },
                            'backgroundColor': 'tomato',
                            'color': 'white',
                        },
                    ],
                    style_cell={'textAlign': 'left'},
                    style_table={'width': '100%'},
                    style_cell_conditional=[
                        {'if': {'column_id': 'product_name'}, 'width': '70%'},
                        {'if': {'column_id': 'quantity'}, 'width': '30%'},
                    ],
                )
            ], width=6),
        ], justify="start", style={'marginTop': '20px'}),
    ], fluid=True)

# Callback to render the appropriate page based on the URL
@dash_app.callback(
    Output('page-content', 'children'),
    [Input('url', 'pathname')]
)
def display_page(pathname):
    # Remove the Dash prefix to simplify path handling
    dash_prefix = '/dashboard/'
    if pathname.startswith(dash_prefix):
        # Extract the relative path
        relative_path = pathname[len(dash_prefix):]
    else:
        relative_path = pathname

    # Route handling based on the relative path
    if relative_path == 'orders':
        return orders_layout()
    elif relative_path == 'recent-purchases':
        return recent_purchases_layout()
    elif relative_path == 'stock-alerts':
        return stock_alerts_layout()
    else:
        return home_layout()


# **Combined Callback to Handle Both Inventory Updates and Filtering**

@dash_app.callback(
    Output('inventory-table', 'data'),
    [
        Input('inventory-table', 'data'),
        Input('inventory-search', 'value')
    ],
    [
        State('inventory-table', 'data_previous')
    ]
)
def manage_inventory_table(table_data, search_value, data_previous):
    ctx = callback_context
    if not ctx.triggered:
        raise PreventUpdate
    triggered_id = ctx.triggered[0]['prop_id'].split('.')[0]

    session = Session()
    try:
        if triggered_id == 'inventory-table':
            # Handle Add Quantity updates
            for new_row, old_row in zip(table_data, data_previous):
                product_name = new_row['product_name']
                add_quantity = new_row.get('add_quantity', '')

                if add_quantity != old_row.get('add_quantity', '') and add_quantity != '':
                    try:
                        add_value = int(add_quantity)
                        if add_value < 0:
                            logger.warning(f"Negative add quantity for {product_name} ignored.")
                            new_row['add_quantity'] = ''
                            continue
                    except ValueError:
                        logger.warning(f"Invalid add quantity input for {product_name}: {add_quantity}")
                        new_row['add_quantity'] = ''
                        continue

                    # Update the quantity in the database
                    inventory_item = session.query(Inventory).filter_by(product_name=product_name).first()
                    if inventory_item:
                        inventory_item.quantity += add_value
                        new_row['quantity'] = inventory_item.quantity
                        new_row['add_quantity'] = ''  # Clear the add_quantity field

                        # Publish the update to MQTT
                        publish_to_mqtt('update', {
                            'product_name': product_name,
                            'quantity': inventory_item.quantity
                        })
                        logger.debug(f"Updated {product_name}: new quantity {inventory_item.quantity}")
                    else:
                        logger.warning(f"Product {product_name} not found in inventory.")
                        new_row['add_quantity'] = ''

            session.commit()  # Commit the session after processing all updates

        # After handling updates, apply the search filter
        if search_value:
            # Fetch inventory items that match the search value
            inventory_item = session.query(Inventory).filter_by(product_name=search_value).first()
            if inventory_item:
                filtered_data = [{
                    "product_name": inventory_item.product_name,
                    "quantity": inventory_item.quantity,
                    "add_quantity": ''
                }]
                logger.debug(f"Filtered inventory data based on search: {filtered_data}")
                return filtered_data
            else:
                logger.debug("No matching product found for the search.")
                return []
        else:
            # If no search value, return all inventory
            updated_inventory = get_inventory_from_db()
            full_data = [{**item, 'add_quantity': ''} for item in updated_inventory]
            logger.debug(f"Returning full inventory data: {full_data}")
            return full_data

    except Exception as e:
        session.rollback()
        logger.error(f"Error managing inventory: {e}")
        raise PreventUpdate
    finally:
        session.close()

# Callback to add new order items dynamically
@dash_app.callback(
    Output('order-items', 'children'),
    [Input('add-item-button', 'n_clicks')],
    [State('order-items', 'children')]
)
def add_order_item(n_clicks, children):
    if n_clicks > 0:
        inventory = get_inventory_from_db()
        casket_options = [{'label': item['product_name'], 'value': item['product_name']} for item in inventory]
        new_item = create_order_item(n_clicks, casket_options)
        children.append(new_item)
        logger.debug(f"Added new order item with index {n_clicks}.")
    return children

# Callback to handle order confirmation
@dash_app.callback(
    Output('order-confirmation', 'children'),
    [Input('confirm-order-button', 'n_clicks')],
    [State('customer-dropdown', 'value'),
     State({'type': 'casket-dropdown', 'index': ALL}, 'value'),
     State({'type': 'quantity-input', 'index': ALL}, 'value')]
)
def confirm_order(n_clicks, customer, casket_list, quantity_list):
    if n_clicks > 0:
        if not customer:
            logger.debug("No customer selected.")
            return dbc.Alert("Please select a customer.", color="danger")

        # Prepare list of items to process
        order_items = []
        for idx, (casket_name, quantity) in enumerate(zip(casket_list, quantity_list)):
            if casket_name and quantity:
                if quantity <= 0:
                    logger.debug(f"Invalid quantity for item {idx + 1}: {quantity}")
                    return dbc.Alert(f"Please enter a valid quantity for item {idx + 1}.", color="danger")
                order_items.append({'casket': casket_name, 'quantity': quantity})
            elif casket_name or quantity:
                logger.debug(f"Incomplete fields for item {idx + 1}.")
                return dbc.Alert(f"Please complete both casket and quantity fields for item {idx + 1}, or leave both empty.", color="danger")

        if not order_items:
            logger.debug("No order items added.")
            return dbc.Alert("Please select at least one casket and quantity to place an order.", color="danger")

        # Process the order
        session = Session()
        try:
            for item in order_items:
                casket_name = item['casket']
                quantity = item['quantity']
                # Fetch the inventory item
                inventory_item = session.query(Inventory).filter_by(product_name=casket_name).first()
                if inventory_item:
                    if inventory_item.quantity >= quantity:
                        # Subtract the quantity
                        inventory_item.quantity -= quantity

                        # Add the purchase to the 'purchase' table
                        purchase = Purchase(
                            customer=customer,
                            product_name=casket_name,
                            quantity=quantity,
                            date_purchased=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        )
                        session.add(purchase)
                    else:
                        logger.debug(f"Insufficient stock for {casket_name}. Available: {inventory_item.quantity}")
                        return dbc.Alert(f"Insufficient stock for {casket_name}. Available: {inventory_item.quantity}", color="danger")
                else:
                    logger.debug(f"Casket {casket_name} not found in inventory.")
                    return dbc.Alert(f"Casket {casket_name} not found in inventory.", color="danger")

            session.commit()
            # Publish updates to MQTT
            for item in order_items:
                casket_name = item['casket']
                quantity = item['quantity']
                inventory_item = session.query(Inventory).filter_by(product_name=casket_name).first()
                if inventory_item:
                    publish_to_mqtt('update', {
                        'product_name': casket_name,
                        'quantity': inventory_item.quantity
                    })
            logger.debug("Order confirmed successfully.")
            return dbc.Alert("Order confirmed successfully!", color="success")
        except Exception as e:
            session.rollback()
            logger.error(f"Error processing order: {e}")
            return dbc.Alert("An error occurred while processing the order.", color="danger")
        finally:
            session.close()
    return ""

# Callback to generate order summary
@dash_app.callback(
    Output('order-summary', 'children'),
    [Input('generate-order-button', 'n_clicks')],
    [State('customer-dropdown', 'value'),
     State({'type': 'casket-dropdown', 'index': ALL}, 'value'),
     State({'type': 'quantity-input', 'index': ALL}, 'value')]
)
def display_order_summary(n_clicks, customer, casket_list, quantity_list):
    if n_clicks > 0:
        if not customer:
            logger.debug("No customer selected for summary.")
            return dbc.Alert("Please select a customer.", color="danger")

        # Prepare list of items to include in the summary
        order_items = []
        for idx, (casket_name, quantity) in enumerate(zip(casket_list, quantity_list)):
            if casket_name and quantity:
                if quantity <= 0:
                    logger.debug(f"Invalid quantity for summary item {idx + 1}: {quantity}")
                    return dbc.Alert(f"Please enter a valid quantity for item {idx + 1}.", color="danger")
                order_items.append({'casket': casket_name, 'quantity': quantity})
            elif casket_name or quantity:
                logger.debug(f"Incomplete fields for summary item {idx + 1}.")
                return dbc.Alert(f"Please complete both casket and quantity fields for item {idx + 1}, or leave both empty.", color="danger")

        if not order_items:
            logger.debug("No order items to summarize.")
            return dbc.Alert("Please select at least one casket and quantity to generate an order summary.", color="danger")

        # Generate order summary
        order_summary = dbc.Container([
            dbc.Row([
                dbc.Col([
                    html.H4("Order Summary"),
                    html.P(f"Customer: {customer}"),
                    html.Ul([html.Li(f"{item['casket']} - Quantity: {item['quantity']}") for item in order_items]),
                    html.Button("Print Order", id='print-button', n_clicks=0, className='btn btn-primary', style={'marginTop': '10px'})
                ], width=6),
            ], justify="start", style={'marginTop': '20px'}),
        ], fluid=True)
        logger.debug("Order summary generated.")
        return order_summary
    return ""

# Client-side callback to trigger the print dialog
dash_app.clientside_callback(
    """
    function(n_clicks) {
        if (n_clicks > 0) {
            window.print();
        }
        return '';
    }
    """,
    Output('print-output', 'children'),
    Input('print-button', 'n_clicks')
)

# Callback to update the recent purchases table based on the filters
@dash_app.callback(
    Output('recent-purchases-table', 'data'),
    [Input('customer-filter', 'value'),
     Input('product-filter', 'value')]
)
def update_recent_purchases_table(customer_filter, product_filter):
    logger.debug(f"Filtering recent purchases with customer: {customer_filter}, product: {product_filter}")
    try:
        query = """
            SELECT customer, product_name, quantity, date_purchased
            FROM purchase
            WHERE date_purchased >= date('now', '-30 days')
        """
        params = []

        if customer_filter:
            placeholders = ','.join('?' for _ in customer_filter)
            query += f" AND customer IN ({placeholders})"
            params.extend(customer_filter)
        if product_filter:
            placeholders = ','.join('?' for _ in product_filter)
            query += f" AND product_name IN ({placeholders})"
            params.extend(product_filter)

        query += " ORDER BY date_purchased DESC"

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        data = [{"customer": row[0], "product_name": row[1], "quantity": row[2], "date_purchased": row[3]} for row in rows]
        logger.debug(f"Filtered recent purchases data: {data}")
        return data
    except Exception as e:
        logger.error(f"Error filtering recent purchases: {e}")
        return []

# Callback to handle stock alerts table updates (if needed)
@dash_app.callback(
    Output('stock-alerts-table', 'data'),
    [Input('stock-alerts-table', 'data_timestamp')]
)
def update_stock_alerts(data_timestamp):
    logger.debug("Updating stock alerts table.")
    try:
        stock_alerts = get_stock_alerts_from_db()
        data = [{"product_name": item['product_name'], "quantity": item['quantity']} for item in stock_alerts]
        logger.debug(f"Updated stock alerts data: {data}")
        return data
    except Exception as e:
        logger.error(f"Error updating stock alerts table: {e}")
        return []

# Run the Flask and Dash app together
if __name__ == '__main__':
    logger.debug("Starting Flask and Dash app with MQTT support")
    try:
        dash_app.run_server(host='0.0.0.0', port=5000, debug=True)
    except Exception as e:
        logger.error(f"Error starting app: {e}")