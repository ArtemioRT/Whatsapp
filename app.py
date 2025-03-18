import os
import time
import json
import logging
import requests

import openai
from flask import Flask, Blueprint, request, jsonify, current_app
from dotenv import load_dotenv
from flasgger import Swagger, swag_from

# Cargar variables de entorno y configurar OpenAI
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
CATALOG_ID = os.getenv("CATALOG_ID")
openai.api_key = OPENAI_API_KEY

# Conjunto para rastrear a los usuarios que ya recibieron el mensaje de bienvenida
SENT_WELCOME = set()

#########################################
# Función para obtener todos los retailer_id
#########################################

def get_all_retailer_ids(catalog_id):
    """
    Consulta el endpoint de consulta de productos y retorna una lista
    con todos los retailer_id de la respuesta.
    Se asume que la respuesta tiene el siguiente formato:
    {
        "data": [
            {
                ...,
                "retailer_id": "valor_deseado",
                ...
            },
            ...
        ]
    }
    """
    endpoint = "https://backend-whatsapp-bp79.onrender.com/ConsultaProductos"
    payload = {"catalog_id": catalog_id}
    try:
        response = requests.post(endpoint, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        products = data.get("data") if isinstance(data, dict) and "data" in data else data
        retailer_ids = []
        if products and isinstance(products, list):
            for product in products:
                retailer_id = product.get("retailer_id")
                if retailer_id:  # Agrega si existe
                    retailer_ids.append(retailer_id)
            if not retailer_ids:
                logging.error("No se encontró ningún retailer_id válido en la respuesta.")
            return retailer_ids
        else:
            logging.error("No se encontraron productos en la respuesta.")
            return []
    except Exception as e:
        logging.error(f"Error al consultar productos: {e}")
        return []

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

def get_catalog_message_input(recipient, text, catalog_id, thread_id=None):
    # Obtener todos los retailer_id desde el endpoint
    retailer_ids = get_all_retailer_ids(catalog_id)
    if not retailer_ids:
        retailer_ids = ["default_id"]

    # Construir la lista de items para el catálogo
    product_items = [{"product_retailer_id": rid} for rid in retailer_ids]

    message_payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",  # Campo requerido
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
                        "product_items": product_items
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

def send_catalog_message(recipient, thread_id=None):
    # Generar la respuesta de catálogo con OpenAI
    catalog_prompt = "Genera un mensaje invitando a explorar el catálogo de productos."
    catalog_text = generate_response(catalog_prompt)
    data = get_catalog_message_input(recipient, catalog_text, catalog_id=CATALOG_ID, thread_id=thread_id)
    return send_message(data)

def send_welcome_message(recipient, thread_id=None):
    # Generar mensaje de bienvenida con OpenAI y enviar imagen de saludo
    welcome_prompt = "Genera un mensaje de bienvenida para un servicio de WhatsApp."
    welcome_text = generate_response(welcome_prompt)
    # Enviar mensaje de bienvenida de texto
    text_data = get_text_message_input(recipient, welcome_text, thread_id=thread_id)
    send_message(text_data)
    # Enviar imagen de saludo (solo en el mensaje de bienvenida)
    image_payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "image",
        "image": {
            "link": "https://t4.ftcdn.net/jpg/04/46/40/87/360_F_446408796_sO3c3ZIuWMgvXNbfXM4Hyqt7pLtGzKQo.jpg"
        }
    }
    if thread_id:
        image_payload["context"] = {"message_id": thread_id}
    send_message(json.dumps(image_payload))

#########################################
# Integración con OpenAI
#########################################

def generate_response(message_body):
    # Se ha eliminado la limitante para mensajes sencillos.
    system_prompt = (
        "Eres un asistente que responde cualquier pregunta basándote en la información disponible. "
        "Cuando sea relevante, incluye en tus respuestas la siguiente información: "
        "locación: Nuevo León, San Pedro; "
        "horarios de recolección: de lunes a sábado de 7am a 10pm; "
        "horarios de atención al cliente: solo en días hábiles; "
        "correo: support jtech@support.mx. "
        "No incluyas información de teléfono. "
        "Si la consulta no puede ser respondida con la información proporcionada, di: "
        "'Lo siento, no tengo la información solicitada'.\n"
        "Responde la siguiente consulta:\n"
    )
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
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
            # Se genera la respuesta de información de la empresa mediante OpenAI
            info_prompt = "Genera una descripción de la empresa y sus servicios."
            info_text = generate_response(info_prompt)
            data = get_text_message_input(wa_id, info_text, thread_id=thread_id)
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
        if message_body in ["/catalogo", "/productos"]:
            send_catalog_message(wa_id, thread_id=thread_id)
        else:
            ai_response = generate_response(message_body)
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
# Blueprints para el Webhook de WhatsApp
#########################################

webhook_blueprint = Blueprint("webhook", __name__)

@webhook_blueprint.route("/webhook", methods=["GET"])
@swag_from({
    'tags': ['Webhook'],
    'operation_summary': 'Verificar suscripción del Webhook',
    'parameters': [
        {
            'name': 'hub.mode',
            'in': 'query',
            'required': False,
            'schema': {'type': 'string'},
            'description': 'Modo de suscripción'
        },
        {
            'name': 'hub.verify_token',
            'in': 'query',
            'required': False,
            'schema': {'type': 'string'},
            'description': 'Token de verificación'
        },
        {
            'name': 'hub.challenge',
            'in': 'query',
            'required': False,
            'schema': {'type': 'string'},
            'description': 'Reto de verificación'
        }
    ],
    'responses': {
        '200': {
            'description': 'Retorna el hub.challenge si la verificación es correcta'
        },
        '403': {
            'description': 'Verificación fallida'
        },
        '400': {
            'description': 'Faltan parámetros'
        }
    }
})
def webhook_get():
    return verify()

@webhook_blueprint.route("/webhook", methods=["POST"])
@swag_from({
    'tags': ['Webhook'],
    'operation_summary': 'Procesar mensajes entrantes de WhatsApp',
    'requestBody': {
        'required': True,
        'content': {
            'application/json': {
                'schema': {
                    'type': 'object',
                    'example': {
                        "object": "whatsapp_business_account",
                        "entry": [
                            {
                                "changes": [
                                    {
                                        "value": {
                                            "messages": [
                                                {
                                                    "id": "wamid...",
                                                    "text": {
                                                        "body": "Hola"
                                                    },
                                                    "from": "5215512345678"
                                                }
                                            ],
                                            "contacts": [
                                                {
                                                    "wa_id": "5215512345678"
                                                }
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
    },
    'responses': {
        '200': {
            'description': 'Mensaje procesado correctamente'
        },
        '400': {
            'description': 'JSON inválido'
        },
        '404': {
            'description': 'No es un evento de WhatsApp API'
        },
        '500': {
            'description': 'Error interno al procesar el mensaje'
        }
    }
})
def webhook_post():
    return handle_message()

def handle_message():
    body = request.get_json()
    # Si es un status update (lectura, entrega, etc.)
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

#########################################
# Creación y configuración de la aplicación Flask
#########################################

def create_app():
    """
    Crea la aplicación Flask y configura el Swagger UI.
    """
    app = Flask(__name__)
    
    # Configuraciones de la API de WhatsApp
    app.config["ACCESS_TOKEN"] = os.getenv("ACCESS_TOKEN")
    app.config["VERSION"] = os.getenv("VERSION", "v20.0")
    app.config["PHONE_NUMBER_ID"] = os.getenv("PHONE_NUMBER_ID")
    app.config["VERIFY_TOKEN"] = VERIFY_TOKEN

    # Configuración de Swagger
    swagger_config = {
        "headers": [],
        "specs": [
            {
                "endpoint": 'apispec',
                "route": '/apispec.json',
                "rule_filter": lambda rule: True,  # Incluir todas las rutas
                "model_filter": lambda tag: True,  # Incluir todos los modelos
            }
        ],
        "static_url_path": "/flasgger_static",
        "swagger_ui": True,
        "specs_route": "/docs/"  # Ruta donde se mostrará la documentación
    }
    swagger_template = {
        "info": {
            "title": "Documentación de la API de WhatsApp",
            "description": (
                "Endpoints para manejar un Webhook de WhatsApp y mostrar/gestionar "
                "mensajes, catálogos y productos. Integra OpenAI para respuestas automáticas."
            ),
            "version": "1.0.0"
        },
        "basePath": "/"
    }
    
    Swagger(app, config=swagger_config, template=swagger_template)
    
    # Registrar el Blueprint para el webhook
    app.register_blueprint(webhook_blueprint)
    
    @app.route("/")
    @swag_from({
        'tags': ['Servidor'],
        'operation_summary': 'Verificar estado del servidor',
        'responses': {
            '200': {
                'description': 'El servidor está activo',
                'content': {
                    'text/plain': {
                        'example': "Servicio activo"
                    }
                }
            }
        }
    })
    def index():
        return "Servicio activo", 200

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
