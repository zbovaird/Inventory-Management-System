import os
import sys
import time
import logging
import json
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
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

# Helper function to get recent purchases from the database
def get_recent_purchases_from_db():
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
    return [{"customer": row[0], "product_name": row[1], "quantity": row[2], "date_purchased": row[3]} for row in rows]

# Helper function to get stock alerts from the database
def get_stock_alerts_from_db():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT product_name, quantity FROM inventory WHERE quantity <= 2")
    rows = cursor.fetchall()
    conn.close()
    return [{"product_name": row[0], "quantity": row[1]} for row in rows]

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
    html.Div(id='inventory-update-output', style={'display': 'none'}),
    html.Div(id='print-output', style={'display': 'none'}),
])

# Home Page Layout
def home_layout():
    inventory = get_inventory_from_db()
    product_names = sorted(set(item['product_name'] for item in inventory))
    product_options = [{'label': name, 'value': name} for name in product_names]

    return dbc.Container([
        # Search bar for caskets (now a dropdown with autocomplete)
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

        # Adjusted DataTable placement and width
        dbc.Row([
            dbc.Col([
                dash_table.DataTable(
                    id='inventory-table',
                    columns=[
                        {"name": "Product Name", "id": "product_name"},
                        {"name": "Quantity", "id": "quantity", "editable": True},
                    ],
                    data=inventory,  # Load data initially
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
            ], width=6),
        ], justify="start"),
    ], fluid=True)

# Add a callback to update the inventory table based on the search input
@dash_app.callback(
    Output('inventory-table', 'data'),
    Input('inventory-search', 'value')
)
def update_inventory_table(search_value):
    if search_value:
        # Fetch inventory items that match the selected product name
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT product_name, quantity FROM inventory WHERE product_name = ?", (search_value,))
        rows = cursor.fetchall()
        conn.close()
        data = [{"product_name": row[0], "quantity": row[1]} for row in rows]
    else:
        # If no search value, return all inventory
        data = get_inventory_from_db()
    return data

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

# Callback to handle order confirmation
@dash_app.callback(
    Output('order-confirmation', 'children'),
    Input('confirm-order-button', 'n_clicks'),
    State('customer-dropdown', 'value'),
    State({'type': 'casket-dropdown', 'index': ALL}, 'value'),
    State({'type': 'quantity-input', 'index': ALL}, 'value'),
)
def confirm_order(n_clicks, customer, casket_list, quantity_list):
    if n_clicks > 0:
        if not customer:
            return dbc.Alert("Please select a customer.", color="danger")

        # Prepare list of items to process
        order_items = []
        for idx, (casket_name, quantity) in enumerate(zip(casket_list, quantity_list)):
            if casket_name and quantity:
                if quantity <= 0:
                    return dbc.Alert(f"Please enter a valid quantity for item {idx + 1}.", color="danger")
                order_items.append({'casket': casket_name, 'quantity': quantity})
            elif casket_name or quantity:
                return dbc.Alert(f"Please complete both casket and quantity fields for item {idx + 1}, or leave both empty.", color="danger")

        if not order_items:
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
                        return dbc.Alert(f"Insufficient stock for {casket_name}. Available: {inventory_item.quantity}", color="danger")
                else:
                    return dbc.Alert(f"Casket {casket_name} not found in inventory.", color="danger")

            session.commit()
            # Display success message
            return dbc.Alert("Order confirmed successfully!", color="success")
        except Exception as e:
            session.rollback()
            app.logger.error(f"Error processing order: {str(e)}")
            return dbc.Alert("An error occurred while processing the order.", color="danger")
        finally:
            session.close()
    return ""

# Callback to handle adding new order items
@dash_app.callback(
    Output('order-items', 'children'),
    Input('add-item-button', 'n_clicks'),
    State('order-items', 'children')
)
def add_order_item(n_clicks, children):
    inventory = get_inventory_from_db()
    casket_options = [{'label': item['product_name'], 'value': item['product_name']} for item in inventory]
    new_item = create_order_item(n_clicks, casket_options)
    children.append(new_item)
    return children

# Callback to generate and display the order summary
@dash_app.callback(
    Output('order-summary', 'children'),
    Input('generate-order-button', 'n_clicks'),
    State('customer-dropdown', 'value'),
    State({'type': 'casket-dropdown', 'index': ALL}, 'value'),
    State({'type': 'quantity-input', 'index': ALL}, 'value'),
)
def display_order_summary(n_clicks, customer, casket_list, quantity_list):
    if n_clicks > 0:
        if not customer:
            return dbc.Alert("Please select a customer.", color="danger")

        # Prepare list of items to include in the summary
        order_items = []
        for idx, (casket_name, quantity) in enumerate(zip(casket_list, quantity_list)):
            if casket_name and quantity:
                if quantity <= 0:
                    return dbc.Alert(f"Please enter a valid quantity for item {idx + 1}.", color="danger")
                order_items.append({'casket': casket_name, 'quantity': quantity})
            elif casket_name or quantity:
                return dbc.Alert(f"Please complete both casket and quantity fields for item {idx + 1}, or leave both empty.", color="danger")

        if not order_items:
            return dbc.Alert("Please select at least one casket and quantity to generate an order summary.", color="danger")

        # Generate order summary
        order_summary = html.Div([
            html.H4("Order Summary"),
            html.P(f"Customer: {customer}"),
            html.Ul([html.Li(f"{item['casket']} - Quantity: {item['quantity']}") for item in order_items]),
            html.Button("Print Order", id='print-button', n_clicks=0, className='btn btn-primary', style={'marginTop': '10px'})
        ], id='printable-area')
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

        # Search filters for customer and product name (now dropdowns with autocomplete)
        dbc.Row([
            dbc.Col([
                html.Label("Filter by Customer"),
                dcc.Dropdown(
                    id='customer-filter',
                    options=customer_options,
                    placeholder='Type to search customers...',
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
                    placeholder='Type to search products...',
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

# Add a callback to update the recent purchases table based on the filters
@dash_app.callback(
    Output('recent-purchases-table', 'data'),
    Input('customer-filter', 'value'),
    Input('product-filter', 'value')
)
def update_recent_purchases_table(customer_filter, product_filter):
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
    return data

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
                                'filter_query': '{quantity} < 2',
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
        ], justify="start"),
    ], fluid=True)

# Update the page content based on the URL
@dash_app.callback(Output('page-content', 'children'),
                   [Input('url', 'pathname')])
def display_page(pathname):
    if pathname == '/orders':
        return orders_layout()
    elif pathname == '/recent-purchases':
        return recent_purchases_layout()
    elif pathname == '/stock-alerts':
        return stock_alerts_layout()
    else:
        return home_layout()

# Callback to handle inventory updates when quantity is edited
@dash_app.callback(
    Output('inventory-update-output', 'children'),
    Input('inventory-table', 'data'),
    State('inventory-table', 'data_previous')
)
def update_inventory(data, data_previous):
    if data_previous is None:
        # First call, no changes
        raise dash.exceptions.PreventUpdate
    else:
        session = Session()
        try:
            for row in data:
                product_name = row['product_name']
                new_quantity = row['quantity']
                # Update the database
                inventory_item = session.query(Inventory).filter_by(product_name=product_name).first()
                if inventory_item:
                    if inventory_item.quantity != new_quantity:
                        inventory_item.quantity = new_quantity
            session.commit()
        except Exception as e:
            session.rollback()
            app.logger.error(f"Error updating inventory: {str(e)}")
        finally:
            session.close()
    return ''

# Run the Flask and Dash app together
if __name__ == '__main__':
    app.logger.debug("Starting Flask and Dash app with MQTT support")
    try:
        dash_app.run_server(host='0.0.0.0', port=5000, debug=True)
    except Exception as e:
        app.logger.error(f"Error starting app: {str(e)}")
