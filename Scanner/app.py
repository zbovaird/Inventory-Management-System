import os
import sys
import time
import logging
import json  # For handling JSON with MQTT
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, Integer, event, text
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError
import paho.mqtt.client as mqtt  # MQTT library

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

# Barcode to product name mapping (mapping based on the first 11 characters)
barcode_prefix_mapping = {
    '71013487523': 'Alex Silver',                 # Alex Silver
    '71011154523': 'Newport Silver',              # Newport Silver
    '71011153523': 'Newport White and Pink',      # Newport White and Pink
    '71011156522': 'Newport Copper',              # Newport Copper
    '71011155523': 'Newport White and Gold',      # Newport White and Gold
    '71075750522': 'Newport Gunmetal',            # Newport Gunmetal
    '71014181523': 'Newport Blue',                # Newport Blue
    '71011151522': 'Newport Ebony and Gold',      # Newport Ebony and Gold
    '71011171523': 'Treemont Blue and Silver',    # Treemont Blue and Silver
    '71011168523': 'Treemont Copper',             # Treemont Copper
    '71063883523': 'Thomas Silver',               # Thomas Silver
    '71085172523': 'Heritage Blue',               # Heritage Blue
    '71075675523': 'Albany Grey',                 # Albany Grey
    '71070314522': 'Emperor 27 White & Pink',     # Emperor 27 White & Pink
    '71070313523': 'Emperor 27 Blue',             # Emperor 27 Blue
    '71011119521': 'Emperor 27 Copper',           # Emperor 27 Copper
    '71011117523': 'Emperor 27 Silver',           # Emperor 27 Silver
}

# List of known internal commands to filter out
internal_commands = [
    "Inventory Mode",
    "Data Upload (For Inventory Mode Only)",
    "Enter Setup",
    "(*)Wireless Adapter Mode",
    "Exit and Save",
    "/*SetFun84*/",  # Add this specific command to your filter
    # Add more internal commands if needed
]

# Function to add or update inventory with retry mechanism for handling database locks
def add_or_update_inventory(session, scanned_barcode):
    retry_count = 5  # Retry up to 5 times if the database is locked
    delay = 0.5      # Delay between retries

    # Filter out known internal commands
    if scanned_barcode in internal_commands:
        app.logger.debug(f"Internal command detected: {scanned_barcode}. Skipping.")
        return 'skipped'

    for attempt in range(retry_count):
        try:
            # Start an IMMEDIATE transaction to prevent further database locks
            session.execute(text('BEGIN IMMEDIATE'))

            # Clean up the barcode and extract the prefix
            scanned_barcode = scanned_barcode.strip()     # Remove extra whitespace
            barcode_prefix = scanned_barcode[:11]         # Get the first 11 characters

            app.logger.debug(f"Scanned barcode: {scanned_barcode}, Prefix: {barcode_prefix}")

            # Ensure product_name is always assigned, even if the prefix is not found
            product_name = barcode_prefix_mapping.get(barcode_prefix, 'Unknown')

            app.logger.debug(f"Mapped product name: {product_name}")

            # Check if the barcode already exists in the inventory
            existing_barcode_item = session.query(Inventory).filter_by(barcode=scanned_barcode).first()

            if existing_barcode_item:
                # Increment the quantity since the barcode exists
                existing_barcode_item.quantity += 1
                app.logger.debug(f"Incremented quantity for {product_name}. New quantity: {existing_barcode_item.quantity}")
                item = existing_barcode_item  # Assign existing item to 'item'
                action = 'updated'
            else:
                # Check if the product_name already exists in the inventory
                item = session.query(Inventory).filter_by(product_name=product_name).first()

                if item:
                    # If the product already exists, increment the quantity
                    item.quantity += 1
                    app.logger.debug(f"Updated inventory for {product_name}. New quantity: {item.quantity}")
                    # Update the barcode for this item
                    item.barcode = scanned_barcode
                    action = 'updated'
                else:
                    # If it doesn't exist, add it with an initial quantity of 1
                    item = Inventory(
                        barcode=scanned_barcode,  # Store the full barcode
                        product_name=product_name,  # Store the mapped product name or 'Unknown'
                        quantity=1
                    )
                    session.add(item)
                    app.logger.debug(f"Added new item to inventory: {product_name}")
                    action = 'added'

            # Commit the changes to the database
            session.commit()

            # Publish update to MQTT
            publish_to_mqtt(action, {
                "barcode": scanned_barcode,
                "product_name": product_name,   # Include the product name in the MQTT message
                "quantity": item.quantity       # Use item's quantity
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
        app.logger.info('Received request at /scan')
        data = request.get_json()
        app.logger.debug(f"Request data: {data}")

        barcode_data = data.get('barcode')
        app.logger.debug(f"Barcode data: {barcode_data}")

        if not barcode_data or not isinstance(barcode_data, str):
            app.logger.warning('Invalid barcode data provided in the request.')
            return jsonify({"error": "Invalid barcode provided."}), 400

        # Add or update inventory using the barcode
        action = add_or_update_inventory(session, barcode_data)

        app.logger.info(f'Barcode processing complete. Action: {action}')
        return jsonify({"status": "success", "action": action}), 200

    except SQLAlchemyError as e:
        app.logger.error("Database error:", exc_info=True)
        return jsonify({"error": "Database error occurred.", "details": str(e)}), 500
    except Exception as e:
        app.logger.error("Error processing request:", exc_info=True)
        return jsonify({"error": "An error occurred while processing the request.", "details": str(e)}), 500
    finally:
        session.close()

# Start the Flask app
if __name__ == '__main__':
    app.logger.debug("Starting Flask app with MQTT support")
    try:
        app.run(host='0.0.0.0', port=5000, debug=True)
    except Exception as e:
        app.logger.error(f"Error starting Flask app: {str(e)}")
