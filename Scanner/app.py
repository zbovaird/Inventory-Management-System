import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import logging
import sys
import json  # For handling JSON with MQTT
from sqlalchemy import create_engine, Column, String, Integer
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
engine = create_engine('sqlite:///' + db_path)
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
SessionFactory = sessionmaker(bind=engine)
Session = scoped_session(SessionFactory)

# Define the MQTT client
broker_url = "test.mosquitto.org"
mqtt_client = mqtt.Client()

# Connect to the MQTT broker
try:
    mqtt_client.connect(broker_url, 1883)  # Default port for MQTT
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

# Barcode to product name mapping
barcode_name_mapping = {
    '110650': 'HN440',  # Example: Model HN440 associated with '110650'
    '856413007606': 'Death Wish Coffee',  # Example: UPC for the coffee product
    # Add more mappings as needed
}

# Function to add or update inventory
def add_or_update_inventory(session, scanned_barcode):
    # Check if the barcode is already in the inventory
    item = session.query(Inventory).filter_by(barcode=scanned_barcode).first()

    product_name = barcode_name_mapping.get(scanned_barcode, 'Unknown')  # Map barcode to product name

    if item:
        # If the item exists, increment the quantity
        item.quantity += 1
        app.logger.debug(f"Updated inventory for {scanned_barcode}. New quantity: {item.quantity}")
        action = 'updated'
    else:
        # If it doesn't exist, add it with an initial quantity of 1
        new_item = Inventory(
            barcode=scanned_barcode,
            product_name=product_name,  # Use mapped product name
            quantity=1
        )
        session.add(new_item)
        app.logger.debug(f"Added new item to inventory: {scanned_barcode}")
        action = 'added'

    # Commit the changes to the database
    session.commit()

    # Publish update to MQTT
    publish_to_mqtt(action, {
        "barcode": scanned_barcode,
        "product_name": product_name,  # Include the product name in the MQTT message
        "quantity": item.quantity if item else 1  # Send the updated quantity
    })

    return action

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
        session.rollback()
        app.logger.error("Database error:", exc_info=True)
        return jsonify({"error": "Database error occurred.", "details": str(e)}), 500  # Include error details
    except Exception as e:
        session.rollback()
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
