import requests

# Define the URL for your Flask backend
BACKEND_URL = 'http://localhost:5000/scan'  # Adjust this URL if Flask is hosted remotely

def send_barcode_to_backend(barcode):
    """Send the scanned barcode to the Flask backend via POST request."""
    data = {"barcode": barcode}
    try:
        response = requests.post(BACKEND_URL, json=data)
        response_data = response.json()
        if response.status_code == 200:
            print(f"Success: {response_data}")
        else:
            print(f"Failed: {response_data}")
    except Exception as e:
        print(f"Error sending barcode to backend: {e}")

def capture_barcode_input():
    """Capture the barcode input from the scanner and send it to the backend."""
    print("Waiting for barcode scan... (Type 'exit' to quit)")
    while True:
        try:
            # Capture the input from the scanner, works like input from a keyboard
            barcode = input("Scan a barcode: ").strip()

            if barcode.lower() == 'exit':
                print("Exiting...")
                break  # Exit the loop if 'exit' is typed

            if barcode:
                # Send the scanned barcode to the backend
                send_barcode_to_backend(barcode)
        except KeyboardInterrupt:
            print("\nInterrupted by user. Exiting...")
            break

if __name__ == "__main__":
    capture_barcode_input()
