Building Inventory Mgmt System (IMS). 
One set of code for using iPhone as barcode scannner, another for using a hardware barcode scanner

iPhone
- front-end index.html file that init camera
- must use https to activate camera
- uses ngrok to tunnel between front and back-end
- back-end is flask with SQLAlchemy
- SQLite used on desktop to connect to inventory.db
- MQTT Explorer set up as MQTT broker to pub/sub from satellite warehouse


Hardware Scanner
- no need for ngrok
- python script run on terminal/cmd that listens for barcode scanner
- receives scans and sends to backend running flask/sqlacademy

- in Backend:
-   barcode is mapped to product name  
-   main warehouse db is updated and avail on sqlite
-   secondary warehouse publishes updates to MQTT broker
-   main warehouse is subscribed to MQTT broker and updates seperate db file for secondary warehouse
-   main warehouse can view seperate db file in sqlite (the secondary warehouse inventory)
