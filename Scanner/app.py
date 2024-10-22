import os
import sys
import time
import logging
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, Integer, event, text
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError
import paho.mqtt.client as mqtt
import dash
from dash import Dash, dcc, html, dash_table
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output, State
import dash_auth
import sqlite3

# Initialize the Flask app
app = Flask(__name__)
CORS(app)

# Set up logging
logging.basicConfig(level=logging.DEBUG)
app.logger.addHandler(logging.StreamHandler(sys.stdout))
app.logger.setLevel(logging.DEBUG)

# Ensure the instance folder exists
instance_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
os.makedirs(instance_path, exist_ok=True)

# Database file path
db_path = os.path.join(instance_path, 'inventory.db')

# Create the SQLite engine and base for SQLAlchemy
engine = create_engine(
    'sqlite:///' + db_path,
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

Base = declarative_base()

# Define the Inventory model (barcode, product name, and quantity)
class Inventory(Base):
    __tablename__ = 'inventory'
    id = Column(Integer, primary_key=True, autoincrement=True)
    barcode = Column(String, unique=True, nullable=False)
    product_name = Column(String)  # Product name mapped from barcode
    quantity = Column(Integer, default=1)

# Create the table (if not exists)
Base.metadata.create_all(engine)

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
except Exception as e:
    app.logger.error(f"Failed to connect to MQTT broker: {e}")

# Function to publish messages to the MQTT broker
def publish_to_mqtt(action, data):
    message = {
        "action": action,
        "data": data
    }
    result = mqtt_client.publish("inventory/updates", json.dumps(message), qos=1)  # QoS 1 for delivery guarantee
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        app.logger.error(f"Failed to publish MQTT message: {result.rc}")

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
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT product_name, quantity FROM inventory")
    rows = cursor.fetchall()
    conn.close()
    return [{"product_name": row[0], "quantity": row[1]} for row in rows]

# Initialize Dash app
dash_app = Dash(__name__, server=app, external_stylesheets=[dbc.themes.LITERA])

# List of valid username/password pairs
VALID_USERNAME_PASSWORD_PAIRS = {
    'user1': 'password1',
    'admin': 'admin123'
}

# Apply authentication only for external requests
@dash_app.server.before_request
def apply_authentication():
    if not (request.remote_addr.startswith('192.168.') or request.remote_addr == '127.0.0.1'):
        auth = dash_auth.BasicAuth(
            dash_app,
            VALID_USERNAME_PASSWORD_PAIRS
        )

# Layout for the Dash app
dash_app.layout = dbc.Container([
    dbc.Row([
        dbc.Col(html.H1("Service Casket Dashboard", style={'color': 'darkblue'}), width=12)
    ], justify="center"),

    # Add Home Button
    dbc.Row([
        dbc.Col([
            html.Button("Home", id='home-button', n_clicks=0, className='btn btn-primary')
        ], width=1),
    ], justify="start", style={'marginTop': '20px'}),

    # Search bar for caskets
    dbc.Row([
        dbc.Col([
            dcc.Input(
                id='inventory-search',
                type='text',
                placeholder='Search by product name...',
                debounce=True,
                style={'width': '100%'}
            ),
        ], width=6),
    ], justify="start", style={'marginTop': '20px'}),

    # Adjusted DataTable placement and width
    dbc.Row([
        dbc.Col([
            dash_table.DataTable(
                id='inventory-table',
                columns=[
                    {"name": "Product Name", "id": "product_name"},
                    {"name": "Quantity", "id": "quantity", "editable": True},
                ],
                data=get_inventory_from_db(),  # Load data initially
                style_data_conditional=[
                    {
                        'if': {
                            'filter_query': '{quantity} < 2',
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
                    {'if': {'column_id': 'product_name'}, 'width': '70%'},
                    {'if': {'column_id': 'quantity'}, 'width': '30%'},
                ],
            )
        ], width=6),  # Adjusted width to 6 out of 12 columns (half the screen)
    ], justify="start"),
], fluid=True)

# Callback to update the inventory table and save changes
@dash_app.callback(
    Output('inventory-table', 'data'),
    Input('inventory-search', 'value'),
    Input('inventory-table', 'data_timestamp'),
    Input('home-button', 'n_clicks'),
    State('inventory-table', 'data'),
)
def update_table(search_value, timestamp, home_clicks, rows):
    ctx = dash.callback_context
    triggered_id = ctx.triggered[0]['prop_id'].split('.')[0]

    if triggered_id == 'inventory-search':
        # Fetch data from database and filter based on search
        inventory = get_inventory_from_db()

        if search_value:
            filtered_inventory = [item for item in inventory if search_value.lower() in item['product_name'].lower()]
        else:
            filtered_inventory = inventory

        return filtered_inventory

    elif triggered_id == 'inventory-table':
        # Save changes to the database
        if rows:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            for row in rows:
                cursor.execute(
                    "UPDATE inventory SET quantity = ? WHERE product_name = ?",
                    (row['quantity'], row['product_name'])
                )

            conn.commit()
            conn.close()

        # Return the updated data
        return rows

    elif triggered_id == 'home-button':
        # Reset the search input and display full inventory
        return get_inventory_from_db()

    else:
        # Initial load or other triggers
        return get_inventory_from_db()

# Function to add or update inventory with retry mechanism for handling database locks
def add_or_update_inventory(session, scanned_barcode):
    retry_count = 5  # Retry up to 5 times if the database is locked
    delay = 0.5      # Delay between retries

    if scanned_barcode in ["Inventory Mode", "Exit and Save"]:
        app.logger.debug(f"Internal command detected: {scanned_barcode}. Skipping.")
        return 'skipped'

    for attempt in range(retry_count):
        try:
            # Start an IMMEDIATE transaction to prevent further database locks
            session.execute(text('BEGIN IMMEDIATE'))

            # Clean up the barcode
            scanned_barcode = scanned_barcode.strip()  # Remove extra whitespace

            # Determine barcode format and extract prefix
            if len(scanned_barcode) >= 14 and '-' in scanned_barcode:
                # Handle 14-character barcode with hyphen
                barcode_prefix = scanned_barcode.split('-')[0]
            else:
                # Handle 11-character or 6-character barcode
                barcode_prefix = scanned_barcode[:11] if len(scanned_barcode) >= 11 else scanned_barcode[:6]

            product_name = barcode_prefix_mapping.get(barcode_prefix, 'Unknown')

            app.logger.debug(f"Scanned barcode: {scanned_barcode}, Prefix: {barcode_prefix}")
            app.logger.debug(f"Mapped product name: {product_name}")

            # Check if the barcode already exists in the inventory
            existing_barcode_item = session.query(Inventory).filter_by(barcode=scanned_barcode).first()

            if existing_barcode_item:
                # Increment the quantity since the barcode exists
                existing_barcode_item.quantity += 1
                item = existing_barcode_item
                action = 'updated'
            else:
                # Check if the product_name already exists in the inventory
                item = session.query(Inventory).filter_by(product_name=product_name).first()

                if item:
                    # If the product already exists, increment the quantity
                    item.quantity += 1
                    # Update the barcode for this item
                    item.barcode = scanned_barcode
                    action = 'updated'
                else:
                    # If it doesn't exist, add it with an initial quantity of 1
                    item = Inventory(
                        barcode=scanned_barcode,
                        product_name=product_name,
                        quantity=1
                    )
                    session.add(item)
                    action = 'added'

            # Commit the changes to the database
            session.commit()

            # Publish update to MQTT
            publish_to_mqtt(action, {
                "barcode": scanned_barcode,
                "product_name": product_name,
                "quantity": item.quantity
            })

            return action

        except SQLAlchemyError as e:
            session.rollback()
            if "database is locked" in str(e):
                app.logger.warning(
                    f"Database is locked, retrying in {delay} seconds... (Attempt {attempt + 1}/{retry_count})"
                )
                time.sleep(delay)
                continue
            else:
                app.logger.error(f"Database error: {str(e)}")
                raise
        except Exception as e:
            session.rollback()
            app.logger.error(f"Error processing barcode: {str(e)}")
            raise
        finally:
            session.close()

    raise Exception("Failed to update inventory after multiple attempts due to database lock.")

# Endpoint to handle barcode scanning
@app.route('/scan', methods=['POST'])
def scan():
    session = Session()
    try:
        data = request.get_json()
        barcode_data = data.get('barcode')

        if not barcode_data or not isinstance(barcode_data, str):
            return jsonify({"error": "Invalid barcode provided."}), 400

        # Add or update inventory using the barcode
        action = add_or_update_inventory(session, barcode_data)

        return jsonify({"status": "success", "action": action}), 200

    except SQLAlchemyError as e:
        app.logger.error("Database error:", exc_info=True)
        return jsonify({"error": "Database error occurred.", "details": str(e)}), 500
    except Exception as e:
        app.logger.error("Error processing request:", exc_info=True)
        return jsonify({"error": "An error occurred while processing the request.", "details": str(e)}), 500
    finally:
        session.close()

# Run the Flask and Dash app together
if __name__ == '__main__':
    app.logger.debug("Starting Flask and Dash app with MQTT support")
    try:
        dash_app.run_server(host='0.0.0.0', port=5000, debug=True)
    except Exception as e:
        app.logger.error(f"Error starting app: {str(e)}")
