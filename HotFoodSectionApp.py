import json
import requests
import aiohttp
import asyncio
import ctypes
import os
from decouple import config
import uuid
import time
import sys
import pyodbc
import qrcode
import threading
import win32print
import win32ui
import win32con
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QLabel, QPushButton, QGridLayout, QListWidget, QListWidgetItem, QMessageBox,
    QDialog, QDialogButtonBox,QSplashScreen,QScroller,QSpacerItem,QSizePolicy
)
from PyQt6.QtWidgets import QSpacerItem, QSizePolicy
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtCore import Qt,QTimer
from PIL import Image, ImageQt, ImageWin
from datetime import datetime
from PyQt6.QtWidgets import QMessageBox


class RetailApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Retail POS For HotFoodSection")
        self.setGeometry(100, 100, 1300, 600)
        
        # Load database credentials from environment variables
        DB_SERVER = config("DB_SERVER")
        DB_NAME = config("DB_NAME")
        DB_USER = config("DB_USER")
        DB_PASSWORD = config("DB_PASSWORD")

        # Initialize database connection
        self.conn_kitchen = self.create_connection(DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD)
        self.cursor_kitchen = self.conn_kitchen.cursor()

        self.device_number = self.load_device_number_from_config()
        self.products = self.load_items()
        self.cart = []
        self.threads = []
        self.running = True
        self.initUI()

    def create_connection(self, server, db, user, password):
        """Create a new database connection."""
        return pyodbc.connect(
            f"Driver={{ODBC Driver 17 for SQL Server}};"
            f"Server={server};"
            f"Database={db};"
            f"uid={user};"
            f"pwd={password};"
            "Encrypt=yes;"  # Enforce encryption
            "TrustServerCertificate=yes;"  # Trust the server certificate
        )

    def execute_query_with_retry(self, query, params=None, max_retries=3, retry_delay=2):
        """
        Execute a query with retry logic in case of connection failure.
        """
        for attempt in range(max_retries):
            try:
                if params:
                    self.cursor_kitchen.execute(query, params)
                else:
                    self.cursor_kitchen.execute(query)
                return self.cursor_kitchen.fetchall()  # or .commit() for INSERT/UPDATE
            except pyodbc.OperationalError as e:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    self.conn_kitchen.close()
                    self.conn_kitchen = self.create_connection(
                        config("DB_SERVER"),
                        config("DB_NAME"),
                        config("DB_USER"),
                        config("DB_PASSWORD")
                    )
                    self.cursor_kitchen = self.conn_kitchen.cursor()
                else:
                    raise  # Re-raise the exception if all retries fail
                
    def load_device_number_from_config(self):
        """Load the device number from the config file."""
        config_file_path = "ConfigDesktopApp.txt"  # Modify to your actual path

        if not os.path.exists(config_file_path):
            raise FileNotFoundError(
                f"Config file {config_file_path} not found.")

        device_number = None
        with open(config_file_path, "r") as config_file:
            for line in config_file:
                if "Device_number" in line:
                    device_number = line.split("=")[1].strip()
                    break

        if not device_number:
            raise ValueError("Device number not found in the config file.")

        return device_number

    def generate_uid(self):
        """Generate UID for the order based on Device Number and timestamp."""
        timestamp = datetime.now().strftime(
            '%Y%m%d%H%M%S')  # Format: YearMonthDayHourMinuteSecond
        uid = f"{self.device_number}.{timestamp}"
        return uid

    def insert_order(self):
        """Insert a new order and return the OrderID using OUTPUT clause."""
        uid = self.generate_uid()

        # Construct the SQL query with OUTPUT clause
        query = """
            INSERT INTO dbo.kitchenOrders
            (UID)
            OUTPUT INSERTED.OrderID
            VALUES (?);
        """

        try:
            # Disable autocommit to manage the transaction manually
            self.conn_kitchen.autocommit = False

            # Execute the insert statement
            self.cursor_kitchen.execute(query, (uid,))
            result = self.cursor_kitchen.fetchone()

            if result:
                order_id = result[0]  # Get the OrderID from the result
                print(f"Order inserted with UID: {uid}, OrderID: {order_id}")  # Debugging: Confirm insertion

                # Update order header, insert order lines, and update order status
                self.update_order_header(order_id)
                self.insert_order_lines(order_id)
                self.update_order_status(order_id)

                # Commit the transaction
                self.conn_kitchen.commit()
                print(f"Order with OrderID {order_id} created successfully.")
                return order_id  # Return the OrderID
            else:
                # Debugging: Print error
                print("Error: Failed to retrieve OrderID.")
                self.conn_kitchen.rollback()  # Rollback the transaction in case of any error
                return None
        except Exception as e:
            print(f"Error inserting order: {e}")  # Handle any insertion errors
            self.conn_kitchen.rollback()  # Rollback the transaction in case of any error
            return None  # Return None if insertion fails
        finally:
            # Ensure that autocommit is turned back on after the operation
            self.conn_kitchen.autocommit = True
            
    def insert_order_lines(self, order_id):
        """Insert each item in the cart as an order line, maintaining the exact sequence of the cart."""
        query = """
        INSERT INTO dbo.kitchenOrdersLines
        (OrderID, ItemCode, Qty, Price, ItemTyp, ParentLineNumber)
        VALUES (?, ?, ?, ?, ?, ?);
        """

        try:
            # Track parent items and their OrderLineIDs
            parent_line_numbers = {}  # {parent_barcode: OrderLineID}

            # Insert items in the exact sequence of the cart
            for item in self.cart:
                item_code = item["barcode"]
                qty = item["quantity"]
                price = item["price"]

                if item["is_parent"]:  # Parent item
                    extras = 'Parent'  # Mark as parent item
                    parent_line_number = 0  # Parent items have ParentLineNumber = 0
                else:  # Extra item
                    extras = 'Extra'  # Mark as extra item
                    parent_uuid = item.get("parent_uuid")
                    if parent_uuid in parent_line_numbers:
                        parent_line_number = parent_line_numbers[parent_uuid]
                    else:
                        raise ValueError(f"Parent UUID {parent_uuid} not found for extra {item_code}")

                # Insert the item
                self.cursor_kitchen.execute(
                    query, (order_id, item_code, qty, price, extras, parent_line_number))

                # Retrieve the OrderLineID of the inserted item
                self.cursor_kitchen.execute("SELECT @@IDENTITY AS OrderLineID;")
                result = self.cursor_kitchen.fetchone()
                if result:
                    order_line_id = result[0]
                    if extras == 'Parent':  # Track OrderLineID for parent items
                        parent_line_numbers[item["uuid"]] = order_line_id
                    print(f"Inserted Item: {item_code}, OrderLineID: {order_line_id}, ParentLineNumber: {parent_line_number}")  # Debugging
                else:
                    raise ValueError(f"Failed to retrieve OrderLineID for item: {item_code}")

            print(f"{len(self.cart)} order lines inserted successfully for OrderID {order_id}.")
            self.conn_kitchen.commit()  # Commit the transaction
        except Exception as e:
            print(f"Error inserting order lines: {e}")
            self.conn_kitchen.rollback()  # Rollback the entire transaction if any line fails
            raise  # Re-raise the exception to indicate failure to insert order lines
    
        
    def update_order_status(self, order_id):
        """Update the status of the order to 'Placed'."""
        try:
            # SQL query to update the status of the order
            update_query = """
            UPDATE dbo.kitchenOrders
            SET Status = 'Placed',
            PlacedTime = GETDATE()
            WHERE OrderID = ?
            """

            # Execute the update statement
            self.cursor_kitchen.execute(update_query, (order_id,))
            self.conn_kitchen.commit()  # Commit the transaction after updating the status
            print(f"Order ID {order_id} status updated to 'Placed'.")
        except pyodbc.DatabaseError as e:
            print(f"Database error updating order status: {e}")
            self.conn_kitchen.rollback()  # Rollback the transaction in case of any error
        except Exception as e:
            print(f"Error updating order status: {e}")
            self.conn_kitchen.rollback()  # Rollback the transaction in case of any error
            
    def update_order_header(self, order_id):
        try:
            # Construct the update query using f-strings for better readability
            update_header_query = f"""
            UPDATE dbo.kitchenOrders
            SET CustomerMobile = '09999990001',
                CustomerName = 'Default RR Customer',
                StoreCode = 42,
                OrderType = 'Desktop',
                PaymentMethod = 'C',
                OrderLines = {len(self.cart)},
                OrderTotal = {sum(item["price"] * item["quantity"] for item in self.cart)}
            WHERE OrderID = ?
            """

            # Execute the update statement
            self.cursor_kitchen.execute(update_header_query, (order_id,))
            self.conn_kitchen.commit()  # Commit the transaction after updating the header
        except pyodbc.DatabaseError as e:
            print(f"Database error updating order header: {e}")
            self.conn_kitchen.rollback()  # Rollback the transaction in case of any error
        except Exception as e:
            print(f"Error updating order header: {e}")
            self.conn_kitchen.rollback()  # Rollback the transaction in case of any error
    @staticmethod
    def resource_path(relative_path):
        """ Get the absolute path to resource, works for dev and for PyInstaller. """
        try:
            if getattr(sys, 'frozen', False):  # If running as a bundled executable
                base_path = sys._MEIPASS
            else:  # If not frozen (i.e., running in development mode)
                base_path = os.path.dirname(os.path.abspath(__file__))
        except Exception:
            base_path = os.path.abspath(".")
        return os.path.join(base_path, relative_path)

    def closeEvent(self, event):
        self.running = False  # Stop threads
        event.accept()

    def load_items(self):
        """Load items from the 'items' table."""
        self.cursor_kitchen.execute(
            "SELECT ItemCode, ItemDesrciptionAR, Price FROM dbo.KitchenItems WHERE ItemTyp != 'Extra' OR ItemTyp = 'Parent' ")
        items = [{"barcode": row[0], "name": row[1], "price": row[2]}
                 for row in self.cursor_kitchen.fetchall()]
        return items

    def load_stylesheet(self, app):
        try:
            if getattr(sys, 'frozen', False):  # If running as a bundled executable
                base_path = sys._MEIPASS
            else:
                base_path = os.path.dirname(os.path.abspath(__file__))

            qss_path = os.path.join(base_path, "styles.qss")
            print(f"Loading QSS file from: {qss_path}")  # Debugging path
            if os.path.exists(qss_path):
                with open(qss_path, "r") as file:
                    app.setStyleSheet(file.read())
                    print("QSS file loaded successfully.")
            else:
                print("Error: QSS file not found.")
        except Exception as e:
            print(f"Error loading QSS file: {e}")

    def load_extras(self):
        """Load extras from the 'extras' table."""
        self.cursor_kitchen.execute(
            "SELECT ItemCode, ItemDesrciptionAR, Price FROM dbo.KitchenItems where ItemTyp='Extra' ")
        extras = [{"barcode": row[0], "name": row[1], "price": row[2]}
                  for row in self.cursor_kitchen.fetchall()]
        return extras

    def initUI(self):
        main_layout = QHBoxLayout()

        # Scrollable Product Grid
        product_widget = QWidget()
        product_layout = QGridLayout()
        product_widget.setLayout(product_layout)

        for index, product in enumerate(self.products):
            button = QPushButton()
            button.setToolTip(f"Price: {product['price']:.2f} L.E")

            # Set the product image
            # Create a vertical layout for the button
            button_layout = QVBoxLayout()

            # Set the product image
            valid_extensions = ['.png', '.jpg', '.jpeg']

            # Try loading the image with different extensions
            for ext in valid_extensions:
                image_path = self.resource_path(
                    # Check with each extension
                    f"images/{product["barcode"].replace(' ', '_')}{ext}")

                if os.path.exists(image_path):
                    pixmap = QPixmap(image_path)
                    if not pixmap.isNull():
                        # Set the image as the button icon if it's loaded successfully
                        # Size for the image
                        pixmap = pixmap.scaled(
                            300, 200, Qt.AspectRatioMode.KeepAspectRatio)
                        icon = QIcon(pixmap)
                        button.setIcon(icon)
                        # Fit the icon to the button size
                        button.setIconSize(pixmap.size())
                        break
                    else:
                        print(f"Image could not be loaded for {
                              product['name']}")
                else:
                    continue

            # Create label for the text (pizza name)
            name_label = QLabel(product["name"])
            name_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)  # Align text to the top and center horizontally
            name_label.setWordWrap(True)
            if len(product["name"]) > 25:
                name_label.setStyleSheet("""
                    font-size: 14px;
                    padding-top: 0px;  /* Remove space above the text */
                    font-weight: bold;
                    border:1px solid #28a745;
                    border-radius:5px;
                    background-color:#28a745;

                """)
                name_label.setFixedHeight(50)  # Set a smaller height for the label
            else:
                name_label.setStyleSheet("""
                font-size: 14px;
                padding-top: 0px;  /* Remove space above the text */
                font-weight: bold;
                border:1px solid #28a745;
                border-radius:5px;
                background-color:#28a745;
                """)
                name_label.setFixedHeight(30)  # Set a smaller height for the label
            # Add the image and the name label to the layout
            button_layout.addWidget(name_label)
            button_layout.setAlignment(Qt.AlignmentFlag.AlignTop)  # Ensure the label is aligned to the top of the button layout

            #button_layout.addWidget(QLabel())  # Add an empty label to take space above the name if necessary

            # Set the layout to the button
            button.setLayout(button_layout)

            button.setFixedSize(160, 150)

            button.clicked.connect(
                lambda _, p=product: self.show_extras_menu(p))
            row = index // 3
            col = index % 3
            product_layout.addWidget(button, row, col)

        product_scroll = QScrollArea()
        product_scroll.setWidgetResizable(True)
        product_scroll.setWidget(product_widget)
        QScroller.grabGesture(product_scroll.viewport(), QScroller.ScrollerGestureType.LeftMouseButtonGesture)
        cart_layout = QVBoxLayout()
        self.cart_label = QLabel("Cart:")
        self.cart_list = QListWidget()
        self.cart_list.setFixedWidth(450)
        self.total_label = QLabel("Total: 0.00 L.E")
        checkout_button = QPushButton("Checkout")
        checkout_button.clicked.connect(self.checkout)
        clear_cart_button = QPushButton("Clear Cart")
        clear_cart_button.clicked.connect(self.clear_cart)

        cart_layout.addWidget(self.cart_label)
        cart_layout.addWidget(self.cart_list)
        cart_layout.addWidget(self.total_label)
        cart_layout.addWidget(checkout_button)
        cart_layout.addWidget(clear_cart_button)
        main_layout.addWidget(product_scroll, 2)
        main_layout.addLayout(cart_layout, 1)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def show_extras_menu(self, product):
        """Show extras submenu with a vertical layout, product image, and adjustable quantity buttons."""
        extras = self.load_extras()
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Extras for {product['name']}")
        dialog.setGeometry(300, 100, 500, 600)  # Increased height to accommodate the image

        # Main layout for the dialog
        layout = QVBoxLayout()

        image_label = QLabel()
        valid_extensions = ['.png', '.jpg', '.jpeg','.ico']
        image_loaded = False

        for ext in valid_extensions:
            image_path = self.resource_path(
                f"images/{product['barcode'].replace(' ', '_')}{ext}")
            if os.path.exists(image_path):
                pixmap = QPixmap(image_path)
                if not pixmap.isNull():
                    pixmap = pixmap.scaled(
                        300, 200, Qt.AspectRatioMode.KeepAspectRatio)
                    image_label.setPixmap(pixmap)
                    image_loaded = True
                    break
                # Try additional image names or patterns
            additional_image_names = [
            product.get("name", "").replace(" ", "_"),  # Use product name
            "default_image",  # A generic default image
            "placeholder",    # A placeholder image
            ]

            for image_name in additional_image_names:
                additional_image_path = self.resource_path(f"images/{image_name}{ext}")
                if os.path.exists(additional_image_path):
                    pixmap = QPixmap(additional_image_path)
                    if not pixmap.isNull():
                        pixmap = pixmap.scaled(300, 200, Qt.AspectRatioMode.KeepAspectRatio)
                        image_label.setPixmap(pixmap)
                        image_loaded = True
                        break
            if image_loaded:
                break
        if not image_loaded:
            image_label.setText("Image not available")
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(image_label)

        # Add the product price label
        self.product_price_label = QLabel(
            f"{product['name']} - {product['price']:.2f} L.E")
        self.product_price_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.product_price_label)

        # Scrollable Extras Layout
        extras_widget = QWidget()
        extras_layout = QVBoxLayout()
        extras_widget.setLayout(extras_layout)

        # Track quantities for each extra
        quantities = {extra['barcode']: 0 for extra in extras}
        quantity_labels = {}  # Dictionary to store QLabel references for each extra

        # Function to update the total price
        def update_total_price():
            """Calculate and update the total price (product price + extras price)."""
            total_price = product['price']  # Start with the product price
            for barcode, qty in quantities.items():
                extra = next(e for e in extras if e['barcode'] == barcode)
                total_price += extra['price'] * qty
            self.product_price_label.setText(
                f"{product['name']} - {total_price:.2f} L.E")

        for extra in extras:
            item_layout = QHBoxLayout()

            # Extra name and price label
            name_label = QLabel(f"{extra['name']} (+{extra['price']:.2f} L.E)")

            # Initialize QLabel dynamically using the value from `quantities`
            initial_quantity = quantities[extra['barcode']]
            quantity_layout = QHBoxLayout()
            quantity_label = QLabel(str(initial_quantity))
            quantity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            quantity_labels[extra['barcode']] = quantity_label

            # Update quantity function
            def update_quantity(barcode, delta):
                """Update the quantity and refresh the QLabel."""
                new_quantity = quantities[barcode] + delta
                if new_quantity >= 0:  # Ensure quantity does not go negative
                    quantities[barcode] = new_quantity
                    quantity_labels[barcode].setText(str(new_quantity))
                    update_total_price()  # Update the total price when quantity changes

            # Buttons for increment and decrement
            plus_button = QPushButton("+")
            plus_button.setFixedSize(25, 25)  # Make buttons smaller
            plus_button.clicked.connect(
                lambda _, b=extra['barcode']: update_quantity(b, 1))

            minus_button = QPushButton("-")
            minus_button.setFixedSize(25, 25)  # Make buttons smaller
            minus_button.clicked.connect(
                lambda _, b=extra['barcode']: update_quantity(b, -1))

            # Add widgets to item layout
            quantity_layout.addWidget(minus_button)
            quantity_layout.addWidget(quantity_label)
            quantity_layout.addWidget(plus_button)

            item_layout.addWidget(name_label, alignment=Qt.AlignmentFlag.AlignLeft)
            item_layout.addLayout(quantity_layout)
            quantity_layout.setContentsMargins(65, 10, 10, 10)
            extras_layout.addLayout(item_layout)
        extras_scroll = QScrollArea()
        extras_scroll.setWidgetResizable(True)
        extras_scroll.setWidget(extras_widget)
        extras_scroll.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents,True)
        QScroller.grabGesture(extras_scroll.viewport(), QScroller.ScrollerGestureType.LeftMouseButtonGesture)
        QScroller.grabGesture(extras_scroll.viewport(), QScroller.ScrollerGestureType.TouchGesture)   
        layout.addWidget(extras_scroll)
        # Add OK and Cancel buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        # Set the main layout for the dialog
        dialog.setLayout(layout)

        # If the dialog is accepted, add the selected extras to the cart
        if dialog.exec():
            selected_extras = [
                {"barcode": barcode, "name": next(e["name"] for e in extras if e["barcode"] == barcode),
                "price": next(e["price"] for e in extras if e["barcode"] == barcode), "quantity": qty}
                for barcode, qty in quantities.items() if qty > 0
            ]
            self.add_to_cart(product, selected_extras)
            
    def add_to_cart(self, product, extras):
        """Add product and selected extras to the cart, creating a new line for each addition."""
        # Add the main product as a new line, even if it's already in the cart
        parent_uuid = str(uuid.uuid4())
        self.cart.append({
            "uuid": parent_uuid,  # Unique identifier for the parent item
            "barcode": product["barcode"],
            "name": product["name"],
            "price": product["price"],
            "quantity": 1,  # Each product added will have quantity 1 for now
            "is_parent": True  # Mark as a parent item
        })

        # Add each extra as a new line, even if it's already in the cart
        for extra in extras:
            self.cart.append({
                "uuid": str(uuid.uuid4()),  # Unique identifier for the extra
                "parent_uuid": parent_uuid,  # Associate with the parent item
                "barcode": extra["barcode"],
                "name": f"{extra['name']}",
                "price": extra["price"],
                "quantity": extra["quantity"],
                "is_parent": False  # Mark as an extra

            })

        # Refresh the cart display
        self.update_cart()

    def update_cart(self):
        """Update the cart list and total price."""
        self.cart_list.clear()
        total_price = 0  # Initialize total price

        for item_data in self.cart:
            cart_item_widget = QWidget()
            cart_item_layout = QHBoxLayout()
            
            item_total_price=item_data["price"]*item_data["quantity"]
            total_price+=item_total_price

            # Create a QLabel for the item name and price
            if item_data["is_parent"]:
                name_label = QLabel(
                f"<span style='font-size: 12pt; font-weight: bold;'>{item_total_price:.2f} L.E</span> "
                f"- {item_data['name']} "
                )
            else:
                name_label = QLabel(
                f"<span style='font-size: 12pt; font-weight: bold;'>{item_total_price:.2f} L.E</span> "
                f"- {item_data['name']} "
                f"<span style='font-size: 12pt; '>(x{item_data['quantity']})</span>"
                )
            name_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            name_label.setStyleSheet("color: black;")

            if item_data["is_parent"]:  # Only add buttons for main items, not extras
                plus_button = QPushButton("+")
                plus_button.setFixedSize(25, 25)
                plus_button.clicked.connect(lambda _, u=item_data['uuid']: self.increment_item(u))
                 # Quantity label
                quantity_label = QLabel(str(item_data['quantity']))
                quantity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                quantity_label.setStyleSheet("font-size: 12pt; font-weight: bold; color: black;")
                # Check if the quantity is 1 to show the recycle bin icon
                 
                if item_data['quantity'] == 1:
                    minus_button = QPushButton()
                    minus_button.setIcon(QIcon(self.resource_path("images/empty1.ico")))  # Set the recycle bin icon
                    minus_button.setFixedSize(25, 25)
                    minus_button.clicked.connect(lambda _, u=item_data['uuid']: self.remove_item(u))
                    
                else:
                    minus_button = QPushButton("-")
                    minus_button.setFixedSize(25, 25)
                    minus_button.clicked.connect(lambda _, u=item_data['uuid']: self.decrement_item(u))
                spacer = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

                # Add widgets to the layout
                cart_item_layout.addItem(spacer) 
                cart_item_layout.addWidget(name_label)
                cart_item_layout.addWidget(minus_button)
                cart_item_layout.addWidget(quantity_label)
                cart_item_layout.addWidget(plus_button)
            else:
                # For extras, add a remove button to the left
                remove_button = QPushButton()
                remove_button.setIcon(QIcon(self.resource_path("images/empty1.ico")))  # Set the recycle bin icon
                remove_button.setFixedSize(25, 25)
                remove_button.clicked.connect(lambda _, u=item_data['uuid']: self.remove_item(u))

                # Add widgets to the layout (remove button first, then name label)
                cart_item_layout.addWidget(remove_button)  # Remove button on the left
                cart_item_layout.addWidget(name_label)

            cart_item_widget.setLayout(cart_item_layout)
            cart_item = QListWidgetItem()
            cart_item.setSizeHint(cart_item_widget.sizeHint())
            self.cart_list.addItem(cart_item)
            self.cart_list.setItemWidget(cart_item, cart_item_widget)

        self.total_label.setText(f"Total: {total_price:.2f} L.E")
        
    def increment_item(self, item_uuid):
        """Increment the quantity of the item and its extras proportionally."""
        # Find the main item
        main_item = next((item for item in self.cart if item['uuid'] == item_uuid), None)
        if main_item:
            # Increment the main item's quantity
            main_item['quantity'] += 1

            # Update the extras proportionally
            for item in self.cart:
                if item.get('parent_uuid') == item_uuid:  # Associated extra
                    # Increment the extra's quantity to match the main item's quantity
                    item['quantity'] = main_item['quantity']

        self.update_cart()

    def decrement_item(self, item_uuid):
        """Decrement the quantity of the item and its extras proportionally."""
        # Find the main item
        main_item = next((item for item in self.cart if item['uuid'] == item_uuid), None)
        if main_item:
            if main_item['quantity'] > 1:
                # Decrement the main item's quantity
                main_item['quantity'] -= 1

                # Update the extras proportionally
                for item in self.cart:
                    if item.get('parent_uuid') == item_uuid:  # Associated extra
                        # Decrement the extra's quantity to match the main item's quantity
                        item['quantity'] = main_item['quantity']

        self.update_cart()
        
    def remove_item(self, item_uuid):
        """Remove the item and all its associated extras from the cart."""
        # Create a new cart list excluding the item and its extras
        new_cart = []

        for item in self.cart:
            # Skip the item and its extras
            if item['uuid'] == item_uuid or (not item["is_parent"] and item.get('parent_uuid') == item_uuid):
                continue  # Skip this item (it's the main item or its extra)
            new_cart.append(item)

        # Update the cart
        self.cart = new_cart
        self.update_cart()
    
    def clear_cart(self):
        """Clear all items from the cart."""
        if not self.cart:
            QMessageBox.information(self, "Checkout", "Your cart is empty.")
            return
        reply = QMessageBox.question(
            self,
            "Clear Cart",
            "Are you sure you want to clear the cart?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.cart = []
            self.update_cart()
            QMessageBox.information(
                self, "Cart Cleared", "Your cart has been cleared.")
        else:
            QMessageBox.information(
                self, "Cancelled", "Cart clearing cancelled.")

    def checkout(self):
        if not self.cart:
            QMessageBox.information(self, "Checkout", "Your cart is empty.")
            return
        reply = QMessageBox.question(
            self,
            "Confirm Checkout",
            "Are you sure you want to proceed with checkout?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            # Generate QR code and show the dialog
            qr_data = "".join(
                f"{item['quantity']}*{item['barcode']}\r" for item in self.cart)
            qr = qrcode.QRCode()
            qr.add_data(qr_data)
            qr.make(fit=True)
            img = qr.make_image(fill="black", back_color="white")
            self.show_qr_code_dialog(img)

            # Send the order to the API
            # self.send_order()
        else:
            QMessageBox.information(
                self, "Cancelled", "Checkout has been Cancelled")

    def show_qr_code_dialog(self, qr_image):
        # Insert the order and get the OrderID
        order_id = self.insert_order()  # This will now return the OrderID
        print(f"Order ID: {order_id}")  # Debugging: Print the returned OrderID

        if not order_id:
            QMessageBox.warning(self, "Error", "Failed to generate order ID.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("QR Code")

        # Use the OrderID as the order number
        order_number = order_id
        # Debugging: Print the order number
        print(f"Order Number: {order_number}")

        qt_image = ImageQt.ImageQt(qr_image)
        pixmap = QPixmap.fromImage(qt_image)
        qr_size = pixmap.size()

        dialog.setFixedSize(qr_size.width() + 20, qr_size.height() + 100)

        layout = QVBoxLayout()
        order_label = QLabel(f"Order No: {order_number}")
        order_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(order_label)
        qr_label = QLabel()
        qr_label.setPixmap(pixmap)
        qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        button_layout = QHBoxLayout()

        print_button = QPushButton("Print QR Code")
        print_button.setFixedSize(150, 40)
        print_button.clicked.connect(
            lambda: self.print_qr_code(pixmap, order_number))

        # Add buttons to the horizontal layout
        button_layout.addWidget(print_button)

        # Add the QR label and button layout to the main layout
        layout.addWidget(qr_label)
        layout.addLayout(button_layout)
        layout.setAlignment(button_layout, Qt.AlignmentFlag.AlignCenter)

        dialog.setLayout(layout)
        dialog.exec()
        self.cart = []
        self.update_cart()

    def print_qr_code(self, pixmap, order_number):
        """Print the QR Code with the order details in a tabular format."""
        def print_receipt(sub_header_text, cart):
            try:
                # Use default printer
                printer_name = win32print.GetDefaultPrinter()
                print(f"Using Printer: {printer_name}")

                # Image path for saving the QR code
                image_path = "temp_qr_code.png"
                pixmap.save(image_path)
                print(f"Saved image to {image_path}")

                if not os.path.exists(image_path):
                    print(f"Error: Image file {image_path} does not exist!")
                    return

                # Open and resize the QR code image
                img = Image.open(image_path)
                new_width = 250  # Set desired QR code width
                new_height = 250  # Set desired QR code height
                img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

                # Initialize the printer
                hprinter = win32print.OpenPrinter(printer_name)
                pdc = win32ui.CreateDC()
                pdc.CreatePrinterDC(printer_name)
                pdc.StartDoc(f"Printing {os.path.basename(image_path)}")
                pdc.StartPage()

                # Font for header (bold and larger)
                header_font = win32ui.CreateFont({
                    "name": "Arial",
                    "height": 50,  # Larger font size
                    "weight": 700,  # Bold
                })
                pdc.SelectObject(header_font)

                # Print header "HF Draft Paper" (centered)
                header_text = "HF Draft Paper"
                header_text_width = pdc.GetTextExtent(header_text)[0]
                header_position = (pdc.GetDeviceCaps(win32con.HORZRES) - header_text_width) // 2
                pdc.TextOut(header_position, 20, header_text)
                y_position = 80  # Move down for the next line

                # Print sub-header (centered)
                sub_header_font = win32ui.CreateFont({
                    "name": "Arial",
                    "height": 30,  # Slightly smaller font size
                    "weight": 700,  # Bold
                })
                pdc.SelectObject(sub_header_font)
                sub_header_text_width = pdc.GetTextExtent(sub_header_text)[0]
                sub_header_position = (pdc.GetDeviceCaps(win32con.HORZRES) - sub_header_text_width) // 2
                pdc.TextOut(sub_header_position, y_position, sub_header_text)
                y_position += 60  # Move down for the next line

                # Define column widths and positions
                column_widths = [200, 100, 150]  # Widths for صنف, كمية, سعر
                x_positions = [50, 250, 350]  # Starting X-positions for each column

                # Font for table headers (bold)
                table_header_font = win32ui.CreateFont({
                    "name": "Arial",
                    "height": 25,
                    "weight": 700,  # Bold
                })
                pdc.SelectObject(table_header_font)

                # Print table headers (centered)
                headers = ["سعر", "كمية", "صنف"]
                for i, header in enumerate(headers):
                    header_text_width = pdc.GetTextExtent(header)[0]
                    centered_position = x_positions[i] + (column_widths[i] - header_text_width) // 2
                    pdc.TextOut(centered_position, y_position, header)
                y_position += 50  # Move down for the table rows
                
                # Print a separator line after headers
                separator = "-" * 50  # Adjust the number of dashes as needed
                separator_width = pdc.GetTextExtent(separator)[0]
                separator_position = (pdc.GetDeviceCaps(win32con.HORZRES) - separator_width) // 2
                pdc.TextOut(separator_position, y_position, separator)
                y_position += 30  # Move down for the table rows

                # Font for table rows (regular)
                table_row_font = win32ui.CreateFont({
                    "name": "Arial",
                    "height": 25,
                    "weight": 700,  # Regular
                })
                pdc.SelectObject(table_row_font)

                # Print cart items in tabular format (centered)
                total_amount = 0
                for item in cart:
                    qty = str(item['quantity'])  # Quantity
                    name = item['name']  # Item name
                    price = f"{item['price']:.2f} L.E"  # Price

                    # Print each column at the centered position
                    name_text_width = pdc.GetTextExtent(name)[0]
                    name_centered_position = x_positions[2] + (column_widths[2] - name_text_width) // 2
                    pdc.TextOut(name_centered_position, y_position, name)  # صنف (Item Name)

                    qty_text_width = pdc.GetTextExtent(qty)[0]
                    qty_centered_position = x_positions[1] + (column_widths[1] - qty_text_width) // 2
                    pdc.TextOut(qty_centered_position, y_position, qty)  # كمية (Quantity)

                    price_text_width = pdc.GetTextExtent(price)[0]
                    price_centered_position = x_positions[0] + (column_widths[0] - price_text_width) // 2
                    pdc.TextOut(price_centered_position, y_position, price)  # سعر (Price)

                    y_position += 55  # Move down for the next row
                    total_amount += item['price'] * item['quantity']  # Calculate total
                # Print a separator line after items
                pdc.TextOut(separator_position, y_position, separator)
                y_position += 30  # Move down for the total amount
                # Print total amount (bold and centered)
                total_font = win32ui.CreateFont({
                    "name": "Arial",
                    "height": 30,
                    "weight": 700,  # Bold
                })
                pdc.SelectObject(total_font)
                total_text = f"  {total_amount:.2f} L.E : الإجمالي"  # Total text
                total_text_width = pdc.GetTextExtent(total_text)[0]  # Calculate text width
                total_position = (pdc.GetDeviceCaps(win32con.HORZRES) - total_text_width) // 2  # Center the text
                pdc.TextOut(total_position, y_position, total_text)
                y_position += 40  # Move down for the next line

                # Print the order number (centered)
                order_number_font = win32ui.CreateFont({
                    "name": "Arial",
                    "height": 30,
                    "weight": 700,  # Bold
                })
                pdc.SelectObject(order_number_font)
                order_number_text = f"Pizza Order No: {order_number}"
                order_number_text_width = pdc.GetTextExtent(order_number_text)[0]
                order_number_position = (pdc.GetDeviceCaps(win32con.HORZRES) - order_number_text_width) // 2
                pdc.TextOut(order_number_position, y_position + 10, order_number_text)
                y_position += 40  # Move down for the QR code

                # Print the QR Code (centered)
                qr_x_position = (pdc.GetDeviceCaps(win32con.HORZRES) - new_width) // 2
                qr_y_position = y_position
                dib = ImageWin.Dib(img_resized)
                dib.draw(pdc.GetHandleOutput(), (qr_x_position, qr_y_position, qr_x_position + new_width, qr_y_position + new_height))

                pdc.EndPage()
                pdc.EndDoc()
                pdc.DeleteDC()
                win32print.ClosePrinter(hprinter)

                print("Printing completed successfully.")

                if os.path.exists(image_path):
                    os.remove(image_path)
                    print(f"Deleted image: {image_path}")
            except Exception as e:
                print(f"Error during printing: {e}")

        sub_header_text = "Please deliver it to the cashier"
        print_receipt(sub_header_text, self.cart)
        sub_header_text = "Keep it with you"
        print_thread = threading.Thread(target=print_receipt, args=(sub_header_text, self.cart), daemon=True)
        print_thread.start()
        self.threads.append(print_thread)
    
    def show_message(self, title, message):
        """Show a QMessageBox safely from the main thread."""
        QMessageBox.information(self, title, message)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    splash_pix = QPixmap(RetailApp.resource_path("Images/seoudi.jpeg"))  # Replace with your splash image path
    splash = QSplashScreen(splash_pix, Qt.WindowType.WindowStaysOnTopHint)
    splash.show()
    window = RetailApp()

    # Simulate loading process
    QTimer.singleShot(2000, splash.close)  # Close splash screen after 2 seconds

    window.load_stylesheet(app)
    window.show()
    sys.exit(app.exec())
