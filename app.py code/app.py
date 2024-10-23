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
import dash
from dash import Dash, dcc, html, dash_table
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
            return redirect(next_page or url_for('index'))
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

# Initialize Dash app
dash_app = Dash(
    __name__,
    server=app,
    external_stylesheets=[dbc.themes.LITERA],
    suppress_callback_exceptions=True  # Suppress exceptions for components not in initial layout
)

# Customer options for the dropdown menu
customer_options = [
    {'label': 'A.S. TURNER & SON FUNERAL HOME', 'value': 'A.S. TURNER & SON FUNERAL HOME'},
    {'label': 'ABBEY FUNERAL HOME', 'value': 'ABBEY FUNERAL HOME'},
    {'label': 'ADAMS FUNERAL HOME', 'value': 'ADAMS FUNERAL HOME'},
    # ... add all customer options here
]

# Application layout with navigation
dash_app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    dbc.NavbarSimple(
        children=[
            dbc.NavItem(dbc.NavLink("Home", href="/")),
            dbc.NavItem(dbc.NavLink("Orders", href="/orders")),
            dbc.NavItem(dbc.NavLink("Recent Purchases", href="/recent-purchases")),
            dbc.NavItem(dbc.NavLink("Stock Alerts", href="/stock-alerts")),
        ],
        brand="Service Casket Dashboard",
        brand_href="/",
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
    if pathname == '/orders':
        return orders_layout()
    elif pathname == '/recent-purchases':
        return recent_purchases_layout()
    elif pathname == '/stock-alerts':
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
    ctx = dash.callback_context
    if not ctx.triggered:
        raise dash.exceptions.PreventUpdate
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

            session.commit()  # **Commit the session after processing all updates**

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
        raise dash.exceptions.PreventUpdate
    finally:
        session.close()

# Removed the separate filter_inventory_table callback

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
