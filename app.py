import os
import time
import json
import logging
import requests

import openai
from flask import Flask, Blueprint, request, jsonify, current_app
from dotenv import load_dotenv

# Cargar variables de entorno y configurar OpenAI
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
CATALOG_ID = os.getenv("CATALOG_ID")
openai.api_key = OPENAI_API_KEY

# Conjunto para rastrear a los usuarios que ya recibieron el mensaje de bienvenida
SENT_WELCOME = set()

#########################################
# Función para obtener product_retailer_id
#########################################

def get_product_retailer_id(catalog_id):
    """
    Consulta el endpoint de consulta de productos y retorna el retailer_id
    del primer producto encontrado.
    """
    endpoint = "https://backend-whatsapp-bp79.onrender.com/ConsultaProductos"
    payload = {"catalog_id": catalog_id}
    try:
        response = requests.post(endpoint, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        # Se asume que la respuesta es una lista de productos o un diccionario con la llave 'data'
        products = data.get("data") if isinstance(data, dict) and "data" in data else data
        if products and isinstance(products, list) and len(products) > 0:
            return products[0].get("retailer_id")
        else:
            logging.error("No se encontraron productos en la respuesta.")
            return None
    except Exception as e:
        logging.error(f"Error al consultar productos: {e}")
        return None

#########################################
# Funciones para armar mensajes de WhatsApp
#########################################

def get_text_message_input(recipient, text, thread_id=None):
    message_payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    }
    if thread_id:
        message_payload["context"] = {"message_id": thread_id}
    return json.dumps(message_payload)

def get_image_message_input(recipient, image_url, caption=None, thread_id=None):
    message_payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "image",
        "image": {
            "link": image_url
        }
    }
    if caption:
        message_payload["image"]["caption"] = caption
    if thread_id:
        message_payload["context"] = {"message_id": thread_id}
    return json.dumps(message_payload)

def get_catalog_message_input(recipient, text, catalog_id=CATALOG_ID, thread_id=None):
    # Obtener el product_retailer_id desde el endpoint de consulta de productos
    product_retailer_id = get_product_retailer_id(catalog_id)
    if not product_retailer_id:
        product_retailer_id = "default_id"  # Valor por defecto en caso de error

    message_payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "interactive",
        "interactive": {
            "type": "product_list",
            "header": {
                "type": "text",
                "text": "Catálogo completo"
            },
            "body": {
                "text": text
            },
            "footer": {
                "text": "Selecciona un producto para más detalles"
            },
            "action": {
                "catalog_id": catalog_id,
                "sections": [
                    {
                        "title": "Sección 1",
                        "product_items": [
                            {"product_retailer_id": product_retailer_id}
                        ]
                    }
                ]
            }
        }
    }
    if thread_id:
        message_payload["context"] = {"message_id": thread_id}
    return json.dumps(message_payload)

def send_message(data):
    headers = {
        "Content-type": "application/json",
        "Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}",
    }
    url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/messages"
    try:
        response = requests.post(url, data=data, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Request failed: {e}")
        return jsonify({"status": "error", "message": "Failed to send message"}), 500
    logging.info(f"Status: {response.status_code}, Body: {response.text}")
    return response

def send_text_with_image(recipient, text, image_url, thread_id=None):
    # Envía un mensaje de texto y, tras una breve espera, la imagen
    text_data = get_text_message_input(recipient, text, thread_id=thread_id)
    send_message(text_data)
    time.sleep(0.5)
    image_data = get_image_message_input(recipient, image_url, thread_id=thread_id)
    send_message(image_data)

def send_catalog_message(recipient, thread_id=None):
    text = "Explora nuestro catálogo completo de productos:"
    data = get_catalog_message_input(recipient, text, catalog_id=CATALOG_ID, thread_id=thread_id)
    return send_message(data)

def send_welcome_message(recipient, thread_id=None):
    welcome_text = "¡Hola! Bienvenido a nuestro servicio de WhatsApp. ¿En qué podemos ayudarte hoy?"
    image_url = "https://t4.ftcdn.net/jpg/04/46/40/87/360_F_446408796_sO3c3ZIuWMgvXNbfXM4Hyqt7pLtGzKQo.jpg"
    send_text_with_image(recipient, welcome_text, image_url, thread_id=thread_id)

#########################################
# Integración con OpenAI
#########################################

def generate_response(message_body):
    # El sistema debe responder únicamente con la información disponible.
    system_prompt = (
        "Eres un asistente que responde únicamente en base a la información disponible. "
        "Si la consulta no puede responderse con la información proporcionada, di: "
        "'Lo siento, no tengo la información solicitada'.\n"
        "Responde la siguiente consulta:\n"
    )
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message_body}
            ],
            max_tokens=400,
            temperature=0.7,
        )
        return response["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.error(f"Error con OpenAI: {e}")
        return "Lo siento, hubo un problema generando la respuesta."

#########################################
# Funciones para manejo de imágenes
#########################################

def should_include_image(message):
    image_keywords = [
        "imagen", "foto", "muestra", "ver", "producto", "catalogo", "catálogo"
    ]
    message_lower = message.lower()
    for keyword in image_keywords:
        if keyword in message_lower:
            return True
    return False

#########################################
# Respuestas interactivas
#########################################

def process_interactive_response(body, thread_id):
    wa_id = body["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"]
    message = body["entry"][0]["changes"][0]["value"]["messages"][0]
    interaction_type = message.get("interactive", {}).get("type")

    if interaction_type == "button_reply":
        button_id = message["interactive"]["button_reply"]["id"]
        if button_id == "catalog":
            send_catalog_message(wa_id, thread_id=thread_id)
        elif button_id == "info":
            text = "Somos una empresa dedicada a..."
            data = get_text_message_input(wa_id, text, thread_id=thread_id)
            send_message(data)
    # Puedes agregar más respuestas interactivas según necesites

#########################################
# Procesamiento principal de mensajes
#########################################

def process_whatsapp_message(body):
    if not is_valid_whatsapp_message(body):
        return
    wa_id = body["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"]
    message = body["entry"][0]["changes"][0]["value"]["messages"][0]
    message_type = message.get("type")
    thread_id = message.get("context", {}).get("id") or message["id"]

    # Enviar mensaje de bienvenida si es el primer mensaje del usuario
    if wa_id not in SENT_WELCOME:
        send_welcome_message(wa_id, thread_id=thread_id)
        SENT_WELCOME.add(wa_id)

    if message_type == "interactive":
        process_interactive_response(body, thread_id)
    elif message_type == "text":
        message_body = message["text"]["body"].lower().strip()
        # Comando para mostrar catálogo
        if message_body in ["/catalogo", "/productos"]:
            send_catalog_message(wa_id, thread_id=thread_id)
        else:
            ai_response = generate_response(message_body)
            if should_include_image(message_body):
                default_image_url = "https://jumpseller.mx/generated/images/learn/los-10-productos-mas-vendidos-en-mexico/online-shopping-mexico-800-3423d44e0.png"
                send_text_with_image(wa_id, ai_response, default_image_url, thread_id=thread_id)
            else:
                data = get_text_message_input(wa_id, ai_response, thread_id=thread_id)
                send_message(data)

def is_valid_whatsapp_message(body):
    try:
        return (
            body.get("object")
            and body.get("entry")
            and body["entry"][0].get("changes")
            and body["entry"][0]["changes"][0].get("value")
            and body["entry"][0]["changes"][0]["value"].get("messages")
            and body["entry"][0]["changes"][0]["value"]["messages"][0]
        )
    except Exception as e:
        logging.error(f"Error validando mensaje: {e}")
        return False

#########################################
# Endpoints del Webhook de WhatsApp
#########################################

webhook_blueprint = Blueprint("webhook", __name__)

def handle_message():
    body = request.get_json()
    # Si se trata de una actualización de estado de WhatsApp, se ignora
    if body.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("statuses"):
        logging.info("Received a WhatsApp status update.")
        return jsonify({"status": "ok"}), 200
    try:
        if is_valid_whatsapp_message(body):
            process_whatsapp_message(body)
            return jsonify({"status": "ok"}), 200
        else:
            return jsonify({"status": "error", "message": "Not a WhatsApp API event"}), 404
    except json.JSONDecodeError:
        logging.error("Failed to decode JSON")
        return jsonify({"status": "error", "message": "Invalid JSON provided"}), 400

def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            logging.info("WEBHOOK_VERIFIED")
            return challenge, 200
        else:
            logging.info("VERIFICATION_FAILED")
            return jsonify({"status": "error", "message": "Verification failed"}), 403
    else:
        logging.info("MISSING_PARAMETER")
        return jsonify({"status": "error", "message": "Missing parameters"}), 400

@webhook_blueprint.route("/webhook", methods=["GET"])
def webhook_get():
    return verify()

@webhook_blueprint.route("/webhook", methods=["POST"])
def webhook_post():
    return handle_message()

#########################################
# Función de creación y configuración de la aplicación Flask
#########################################

def create_app():
    app = Flask(__name__)
    
    # Configuración de variables necesarias en current_app.config
    app.config["ACCESS_TOKEN"] = os.getenv("ACCESS_TOKEN")
    app.config["VERSION"] = os.getenv("VERSION", "v20.0")
    app.config["PHONE_NUMBER_ID"] = os.getenv("PHONE_NUMBER_ID")
    app.config["VERIFY_TOKEN"] = VERIFY_TOKEN

    # Registrar blueprint
    app.register_blueprint(webhook_blueprint)
    
    # Endpoint raíz para indicar que el servicio está activo
    @app.route("/")
    def index():
        return "Servicio activo", 200

    return app

# Si se ejecuta directamente en modo desarrollo
if __name__ == '__main__':
    app = create_app()
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
