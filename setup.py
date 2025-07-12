from flask import Flask, jsonify, request, render_template, send_file, current_app
import requests
import json
import logging
from pymongo import MongoClient
from datetime import datetime
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
import pandas as pd
import io
import os
import sys
import webbrowser
from threading import Timer
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Determine if we're running in a bundle
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    app = Flask(__name__, template_folder=template_folder)
else:
    app = Flask(__name__)

CORS(app, resources={r"/api/*": {"origins": "*"}})
app.wsgi_app = ProxyFix(app.wsgi_app)

# Configure logging to file
log_file = os.path.join(os.path.expanduser("~"), 'mercadolibre_search.log')
logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add console handler for development
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
logger.addHandler(console_handler)

# MongoDB configuration
MONGO_URI = 'mongodb://localhost:27017/'
DB_NAME = 'multi_ecommerce_db'
COLLECTION_NAME = 'productos'

# Caching configuration
cache = {}

def open_browser():
    """Open the default web browser to the application URL"""
    webbrowser.open('http://127.0.0.1:5000/')

def get_mongodb_connection():
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.server_info()
        db = client[DB_NAME]
        return client, db
    except Exception as e:
        logger.error(f"Error connecting to MongoDB: {e}")
        return None, None

def buscar_producto_api(producto, offset=0, limit=50):
    cache_key = f"{producto}_{offset}_{limit}"
    
    # Check if result is cached
    if cache_key in cache:
        logger.debug(f"Retrieving cached results for {producto} (offset: {offset}, limit: {limit})")
        return cache[cache_key]
    
    try:
        url = f'https://api.mercadolibre.com/sites/MLM/search'
        params = {
            'q': producto,
            'limit': limit,
            'offset': offset
        }
        logger.debug(f"Buscando producto: {producto} (offset: {offset}, limit: {limit})")
        
        # Add rate limiting
        time.sleep(0.5)  # Respect API rate limits
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        result = response.json()
        
        # Cache the result
        cache[cache_key] = result
        
        return result
    except requests.exceptions.RequestException as e:
        logger.error(f"Error buscando producto en API: {e}")
        return None

def procesar_lote(producto, offset, batch_size):
    try:
        resultado = buscar_producto_api(producto, offset, batch_size)
        if not resultado or 'results' not in resultado:
            return []
            
        productos_procesados = []
        for item in resultado['results']:
            try:
                # First try to get seller_id to fetch sold items
                seller_id = item.get('seller', {}).get('id')
                sold_quantity = 0
                
                if seller_id:
                    try:
                        # Get seller information including sold items
                        seller_url = f'https://api.mercadolibre.com/items/{item["id"]}'
                        seller_response = requests.get(seller_url, timeout=10)
                        if seller_response.status_code == 200:
                            seller_data = seller_response.json()
                            sold_quantity = seller_data.get('sold_quantity', 0)
                    except Exception as e:
                        logger.error(f"Error getting seller data: {e}")
                
                producto = {
                    "producto": item['title'],
                    "plataforma": "mercadolibre",
                    "precio_original": float(item['price']),
                    "precio_con_descuento": float(item.get('price', item.get('original_price', item['price']))),
                    "descuento": float(item.get('discount_percentage', 0)),
                    "vendedor": item.get('seller', {}).get('nickname', 'N/A'),
                    "cuotas": int(item.get('installments', {}).get('quantity', 0)),
                    "meses_intereses": int(item.get('installments', {}).get('months', 0)),
                    "envio_gratis": bool(item.get('shipping', {}).get('free_shipping', False)),
                    "estado_producto": item.get('condition', 'N/A'),
                    "cantida_vendido": sold_quantity,
                    "cantidad_disponible": int(item.get('available_quantity', 0)),
                    "url_producto": item['permalink'],
                    "imagen_url": item['thumbnail'],
                    "fecha_extraccion": datetime.now(),
                    "categoria": item.get('category_id', 'N/A')
                }
                productos_procesados.append(producto)
                
                # Add rate limiting between item requests
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Error procesando producto: {e}")
                continue
                
        return productos_procesados
    except Exception as e:
        logger.error(f"Error en procesar_lote: {e}")
        return []

def guardar_productos_en_db(datos_productos, batch_size=100):
    if not datos_productos:
        logger.warning("No hay productos para guardar")
        return False
        
    try:
        client, db = get_mongodb_connection()
        if not client or not db:
            logger.error("No se pudo conectar a MongoDB")
            return False
            
        collection = db[COLLECTION_NAME]
        
        # Process in batches
        for i in range(0, len(datos_productos), batch_size):
            lote = datos_productos[i:i + batch_size]
            operaciones = [
                {
                    'update_one': {
                        'filter': {
                            'producto': producto['producto'],
                            'plataforma': producto['plataforma']
                        },
                        'update': {'$set': producto},
                        'upsert': True
                    }
                }
                for producto in lote
            ]
            
            collection.bulk_write(operaciones, ordered=False)
            
        client.close()
        logger.info(f"Guardados exitosamente {len(datos_productos)} productos en la base de datos")
        return True
    except Exception as e:
        logger.error(f"Error guardando productos en MongoDB: {e}")
        return False

def generate_excel(productos):
    """Generate Excel file from products data"""
    try:
        # Create DataFrame with selected columns
        df = pd.DataFrame(productos)
        
        # Reorder and rename columns for better presentation
        columns_map = {
            'producto': 'Producto',
            'precio_original': 'Precio Original',
            'precio_con_descuento': 'Precio con Descuento',
            'descuento': 'Descuento (%)',
            'vendedor': 'Vendedor',
            'estado_producto': 'Estado del Producto',
            'cantida_vendido': 'Cantidad Vendida',
            'cuotas': 'Cuotas Disponibles',
            'meses_intereses': 'Meses sin Intereses',
            'envio_gratis': 'Envío Gratis',
            'cantidad_disponible': 'Cantidad Disponible',
            'url_producto': 'URL del Producto',
            'categoria': 'Categoría'
        }
        
        df = df[list(columns_map.keys())]
        df = df.rename(columns=columns_map)
        
        # Create Excel file in memory
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, sheet_name='Productos', index=False)
            
            # Get workbook and worksheet objects
            workbook = writer.book
            worksheet = writer.sheets['Productos']
            
            # Add formats
            header_format = workbook.add_format({
                'bold': True,
                'bg_color': '#1e88e5',
                'color': 'white',
                'align': 'center',
                'valign': 'vcenter',
                'border': 1
            })
            
            # Apply formats
            for col_num, value in enumerate(df.columns.values):
                worksheet.write(0, col_num, value, header_format)
                worksheet.set_column(col_num, col_num, 15)  # Set column width
            
            # Set URL column width
            url_col = list(columns_map.keys()).index('url_producto')
            worksheet.set_column(url_col, url_col, 50)
            
            # Set product name column width
            producto_col = list(columns_map.keys()).index('producto')
            worksheet.set_column(producto_col, producto_col, 40)
        
        output.seek(0)
        return output
    except Exception as e:
        logger.error(f"Error generando archivo Excel: {e}")
        return None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def mercadoLibre():
    try:
        logger.debug("Solicitud de búsqueda recibida")
        data = request.get_json()
        if not data or "producto" not in data:
            logger.warning("Datos de solicitud inválidos")
            return jsonify({"error": "Datos del producto no proporcionados"}), 400
        
        producto = data["producto"]
        limite = data.get('limit', 50)
        offset = data.get('offset', 0)
        num_batches = data.get('num_batches', 2)  # Default is 2 batches

        batch_size = limite // num_batches
        productos_total = []
        
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(procesar_lote, producto, offset + i * batch_size, batch_size)
                for i in range(num_batches)
            ]
            
            for future in as_completed(futures):
                productos_total.extend(future.result())
                
        logger.debug(f"Productos procesados: {len(productos_total)}")

        # Guardar en base de datos
        guardar_productos_en_db(productos_total)

        # Respuesta exitosa
        return jsonify({"productos": productos_total, "mensaje": "Busqueda completada con éxito"}), 200
    except Exception as e:
        logger.error(f"Error en la solicitud de búsqueda: {e}")
        return jsonify({"error": "Ocurrió un error durante la búsqueda"}), 500

@app.route('/api/download', methods=['GET'])
def download():
    try:
        producto = request.args.get('producto')
        if not producto:
            logger.warning("Parámetro de producto faltante")
            return jsonify({"error": "Falta el parámetro de producto"}), 400

        client, db = get_mongodb_connection()
        if not client or not db:
            return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

        collection = db[COLLECTION_NAME]
        productos = list(collection.find({"producto": {"$regex": producto, "$options": "i"}}))

        client.close()

        if not productos:
            return jsonify({"error": "No se encontraron productos"}), 404

        # Generate Excel
        excel_output = generate_excel(productos)
        if not excel_output:
            return jsonify({"error": "Error generando archivo Excel"}), 500

        return send_file(
            excel_output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            attachment_filename=f'productos_{producto}_{datetime.now().strftime("%Y%m%d")}.xlsx'
        )
    except Exception as e:
        logger.error(f"Error en la descarga de archivo: {e}")
        return jsonify({"error": "Error al descargar el archivo"}), 500

if __name__ == '__main__':
    port = 5000
    Timer(1, open_browser).start()
    app.run(debug=False, port=port)
