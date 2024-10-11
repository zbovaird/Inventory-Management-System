Building Inventory Mgmt System (IMS)
One set code for using iPhone as barcode scannner, another for using a hardware barcode scanner

iPhone
- front-end index.html file that init camera
- must use https to activate camera
- uses ngrok to tunnel between front and back-end
- back-end is flask with SQLAlchemy
- SQLite used on desktop to connect to inventory.db
- MQTT Explorer set up as MQTT broker to pub/sub from satellite warehouse
