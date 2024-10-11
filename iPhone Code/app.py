import os
from flask import Flask, request, jsonify, render_template, make_response
from flask_cors import CORS
import logging
import sys
import re  # Import regular expressions module
import json  # For handling JSON with MQTT
from sqlalchemy import create_engine, Column, String, Integer
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError
import paho.mqtt.client as mqtt  # Importing MQTT library

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

# Create the engine and base
engine = create_engine('sqlite:///' + db_path)
Base = declarative_base()

# Define the Inventory model
class Inventory(Base):
    __tablename__ = 'inventory'
    id = Column(Integer, primary_key=True, autoincrement=True)
    barcode = Column(String, unique=True, nullable=False)
    name = Column(String)  # New column for the name
    make = Column(String)
    model = Column(String)
    color = Column(String)
    quantity = Column(Integer, default=1)

# Create tables (if not exists)
Base.metadata.create_all(engine)

# Create a scoped session
SessionFactory = sessionmaker(bind=engine)
Session = scoped_session(SessionFactory)

# Define MQTT client
broker_url = "test.mosquitto.org"  # Use Mosquitto's public broker for now
mqtt_client = mqtt.Client()

# Connect to the MQTT broker with error handling
try:
    mqtt_client.connect(broker_url, 1883)  # Default port for MQTT
except Exception as e:
    app.logger.error(f"Failed to connect to MQTT broker: {e}")

# Function to publish messages to MQTT
def publish_to_mqtt(action, data):
    message = {
        "action": action,
        "data": data
    }
    result = mqtt_client.publish("inventory/updates", json.dumps(message), qos=1)  # QoS 1 for delivery guarantee
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        app.logger.error(f"Failed to publish MQTT message: {result.rc}")

# Define a function to determine barcode type
def determine_barcode_type(barcode):
    # Simple determination based on length
    if len(barcode) == 8:
        return "EAN-8"
    elif len(barcode) == 13:
        return "EAN-13"
    elif len(barcode) == 12:
        return "UPC-A"
    elif len(barcode) == 14:
        return "GTIN-14"
    else:
        return "CODE-128"

# Extract make/model from barcode
def extract_make_model(barcode):
    # Check if the barcode contains a hyphen (e.g., '110650-2311164')
    if '-' in barcode:
        make_model_code = barcode.split('-', 1)[0]
    else:
        make_model_code = barcode  # Treat the entire barcode as make_model_code for UPC
    return make_model_code

# Define a mapping of make_model_code to product names
barcode_name_mapping = {
    '110650': 'HN440',   # Model HN440 associated with '110650-XXXXXXX'
    '856413007606': 'Death Wish Coffee',  # UPC for the coffee product
    # Add other mappings as needed
}

# Function to add or update inventory
def add_or_update_inventory(session, scanned_barcode, name=None, make=None, model=None, color=None):
    # Check if the barcode is already in the inventory
    item = session.query(Inventory).filter_by(barcode=scanned_barcode).first()
    
    if item:
        # If it exists, increment the quantity
        item.quantity += 1
        app.logger.debug(f"Updated inventory for {scanned_barcode}. New quantity: {item.quantity}")
        action = 'updated'
    else:
        # If it doesn't exist, add it with the initial quantity of 1
        new_item = Inventory(
            barcode=scanned_barcode,
            name=name,       # Include the name
            make=make,
            model=model,
            color=color,
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
        "name": name,
        "make": make,
        "model": model,
        "color": color,
        "quantity": item.quantity if item else 1
    })
    
    return action

@app.route('/')
def home():
    app.logger.debug("Home route accessed")
    response = make_response(render_template('index.html'))
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response

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

        # Extract the make/model code from the barcode
        make_model_code = extract_make_model(barcode_data)
        if not make_model_code:
            return jsonify({"error": "Invalid barcode format."}), 400
        app.logger.debug(f"Extracted make/model code: {make_model_code}")

        # Determine the barcode type
        barcode_type = determine_barcode_type(barcode_data)

        # Get the name associated with the barcode
        name = barcode_name_mapping.get(make_model_code, 'Unknown')  # Use mapping or default to 'Unknown'

        # Optionally, map make_model_code to actual make and model
        make = 'Unknown'  # Or extract based on make_model_code
        model = 'Unknown'  # Or extract based on make_model_code

        # Add or update inventory using make_model_code as the barcode
        action = add_or_update_inventory(
            session,
            make_model_code,
            name=name,       # Pass the name
            make=make,
            model=model,
            color='Unknown'
        )

        app.logger.info(f'Barcode processing complete. Action: {action}')
        return jsonify({"status": "success", "action": action, "barcode_type": barcode_type}), 200

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

if __name__ == '__main__':
    app.logger.debug("Starting Flask app with ngrok and MQTT")
    try:
        app.run(host='0.0.0.0', port=5000, debug=True)
    except Exception as e:
        app.logger.error(f"Error starting Flask app: {str(e)}")
