import os
import paho.mqtt.client as mqtt
import json
from flask import Flask, request, jsonify
from sqlalchemy import create_engine, Column, String, Integer
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

app = Flask(__name__)

# SQLite database for the second warehouse
db_path = os.path.join('instance', 'inventory_warehouse2.db')
engine = create_engine('sqlite:///' + db_path)
Base = declarative_base()

# Inventory model
class Inventory(Base):
    __tablename__ = 'inventory'
    id = Column(Integer, primary_key=True, autoincrement=True)
    barcode = Column(String, unique=True, nullable=False)
    product_name = Column(String)
    quantity = Column(Integer, default=1)

Base.metadata.create_all(engine)

# MQTT client setup
broker_url = "test.mosquitto.org"
mqtt_client = mqtt.Client()

def publish_to_mqtt_warehouse2(action, data):
    message = {
        "action": action,
        "data": data
    }
    mqtt_client.connect(broker_url, 1883)
    mqtt_client.publish("warehouse2/inventory/updates", json.dumps(message), qos=1)

# Create a scoped session
SessionFactory = sessionmaker(bind=engine)
Session = scoped_session(SessionFactory)

# Route for scanning and updating the database
@app.route('/scan', methods=['POST'])
def scan():
    session = Session()
    try:
        data = request.get_json()
        barcode = data.get('barcode')

        # Check if the barcode exists in the database
        item = session.query(Inventory).filter_by(barcode=barcode).first()
        if item:
            item.quantity += 1
            action = 'updated'
        else:
            product_name = "Mapped Product"  # Map barcode to product name as needed
            new_item = Inventory(barcode=barcode, product_name=product_name, quantity=1)
            session.add(new_item)
            action = 'added'

        # Commit to the local database
        session.commit()

        # Publish the update to MQTT
        publish_to_mqtt_warehouse2(action, {"barcode": barcode, "product_name": item.product_name if item else product_name, "quantity": item.quantity if item else 1})

        return jsonify({"status": "success", "action": action})
    finally:
        session.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
