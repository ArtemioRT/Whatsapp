import os
import time
import json
import logging
import requests
import uuid
import asyncio

from flask import Flask, Blueprint, request, jsonify, current_app
from dotenv import load_dotenv

from agents import Agent, Runner, set_tracing_export_api_key, function_tool

# Configurar logging para depuración
logging.basicConfig(level=logging.INFO)

# Cargar variables de entorno
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
CATALOG_ID = os.getenv("CATALOG_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERSION = os.getenv("VERSION", "v20.0")

# Configurar API key para tracing, si aplica
set_tracing_export_api_key(OPENAI_API_KEY)

# Conjunto para rastrear a los usuarios que ya recibieron el mensaje de bienvenida
SENT_WELCOME = set()

# Definición de las tools
@function_tool
def get_product_retailer_id(catalog_id: str) -> str:
    """
    Consulta un endpoint externo para obtener el retailer_id del primer producto
    del catálogo. Si no encuentra nada, retorna un string por defecto.
    """
    endpoint = "https://backend-whatsapp-bp79.onrender.com/ConsultaProductos"
    payload = {"catalog_id": catalog_id}
    try:
        response = requests.post(endpoint, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        products = data.get("data") if isinstance(data, dict) and "data" in data else data
        if products and isinstance(products, list) and len(products) > 0:
            return products[0].get("retailer_id")
        else:
            logging.error("No se encontraron productos en la respuesta.")
            return "default_id"
    except Exception as e:
        logging.error(f"Error al consultar productos: {e}")
        return "default_id"

@function_tool
def send_message_json(payload: dict) -> dict:
    """
    Envía un mensaje a la API de WhatsApp Business con el JSON recibido en 'payload'.
    Retorna el status_code y texto de la respuesta para fines de depuración.
    """
    headers = {
        "Content-type": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"
    logging.info(f"Enviando payload a WhatsApp: {json.dumps(payload)}")
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        logging.info(f"Respuesta de WhatsApp: {response.status_code} - {response.text}")
        return {"status": response.status_code, "response_text": response.text}
    except requests.RequestException as e:
        logging.error(f"Error al enviar mensaje a WhatsApp: {e}")
        return {"error": str(e)}

@function_tool
def generate_openai_response(message_body: str) -> str:
    """
    Llama a la API de OpenAI para generar una respuesta en base a un prompt de sistema.
    """
    import openai
    openai.api_key = OPENAI_API_KEY

    system_prompt = (
        "Eres un asistente que responde únicamente con la información disponible. "
        "Si la consulta no puede responderse con la información proporcionada, di: "
        "'Lo siento, no tengo la información solicitada'."
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

@function_tool
def build_text_message(recipient: str, text: str, thread_id: str = None) -> dict:
    """
    Construye el payload JSON para enviar un mensaje de texto.
    """
    payload = {
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
        payload["context"] = {"message_id": thread_id}
    return payload

@function_tool
def build_image_message(recipient: str, image_url: str, caption: str = None, thread_id: str = None) -> dict:
    """
    Construye el payload JSON para enviar una imagen con texto opcional.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "image",
        "image": {
            "link": image_url
        }
    }
    if caption:
        payload["image"]["caption"] = caption
    if thread_id:
        payload["context"] = {"message_id": thread_id}
    return payload

@function_tool
def build_catalog_message(
    recipient: str,
    text: str,
    catalog_id: str = CATALOG_ID,
    thread_id: str = None
) -> dict:
    """
    Construye el payload JSON para enviar un listado interactivo de productos (catálogo).
    """
    retailer_id = get_product_retailer_id(catalog_id)
    payload = {
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
                            {"product_retailer_id": retailer_id}
                        ]
                    }
                ]
            }
        }
    }
    if thread_id:
        payload["context"] = {"message_id": thread_id}
    return payload

# Definir agente
class WhatsAppAgent(Agent):
    """
    Agente que decide cómo responder a mensajes de WhatsApp usando las herramientas registradas.
    """
    def run(self, user_input: dict) -> str:
        """
        user_input: Se espera un dict con los campos necesarios, por ejemplo:
          {
            "wa_id": "...",
            "message_body": "...",
            "thread_id": "...",
            "is_interactive": bool,
            "button_id": "...",
          }
        Retorna un texto que describe la acción tomada (para logging).
        """
        wa_id = user_input["wa_id"]
        message_body = user_input["message_body"]
        thread_id = user_input["thread_id"]

        # Enviar mensaje de bienvenida si es la primera vez que escribe
        if wa_id not in SENT_WELCOME:
            welcome_text = "¡Hola! Bienvenido a nuestro servicio de WhatsApp. ¿En qué podemos ayudarte hoy?"
            image_url = "https://t4.ftcdn.net/jpg/04/46/40/87/360_F_446408796_sO3c3ZIuWMgvXNbfXM4Hyqt7pLtGzKQo.jpg"
            payload_txt = build_text_message(wa_id, welcome_text, thread_id)
            send_message_json(payload_txt)
            time.sleep(0.5)
            payload_img = build_image_message(wa_id, image_url, None, thread_id)
            send_message_json(payload_img)
            SENT_WELCOME.add(wa_id)

        # Si es un mensaje interactivo (botón o catálogo)
        if user_input["is_interactive"]:
            button_id = user_input.get("button_id", "")
            if button_id == "catalog":
                cat_msg = build_catalog_message(
                    wa_id,
                    "Explora nuestro catálogo completo de productos:",
                    CATALOG_ID,
                    thread_id
                )
                send_message_json(cat_msg)
                return "Enviado catálogo vía botón"
            elif button_id == "info":
                info_payload = build_text_message(wa_id, "Somos una empresa dedicada a...", thread_id)
                send_message_json(info_payload)
                return "Enviada info vía botón"
            else:
                return "Botón no reconocido"

        # Si es texto normal
        if message_body in ["/catalogo", "/productos"]:
            cat_msg = build_catalog_message(
                wa_id,
                "Explora nuestro catálogo completo de productos:",
                CATALOG_ID,
                thread_id
            )
            send_message_json(cat_msg)
            return "Enviado catálogo por comando"
        # Verificar si se recibió un mensaje vacío
        elif not message_body.strip():
            default_response = "No se recibió ningún mensaje. Por favor, envía una consulta válida."
            txt_payload = build_text_message(wa_id, default_response, thread_id)
            send_message_json(txt_payload)
            return "Mensaje vacío, respuesta enviada"
        else:
            ai_response = generate_openai_response(message_body)
            txt_payload = build_text_message(wa_id, ai_response, thread_id)
            send_message_json(txt_payload)
            return "Respuesta solo texto"

# Instanciar el agente y el Runner
whatsapp_agent = WhatsAppAgent(name="WhatsApp Assistant")
runner = Runner()  # Se instancia sin argumentos

# Funciones de procesamiento del webhook
def is_valid_whatsapp_message(body: dict) -> bool:
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

def process_whatsapp_message(body: dict):
    value = body["entry"][0]["changes"][0]["value"]
    wa_id = value["contacts"][0]["wa_id"]
    message = value["messages"][0]
    message_type = message.get("type")
    thread_id = message.get("context", {}).get("id") or message["id"]

    user_input = {
        "wa_id": wa_id,
        "thread_id": thread_id,
        "is_interactive": (message_type == "interactive"),
        "button_id": None,
        "message_body": ""
    }

    if message_type == "interactive":
        interaction_type = message["interactive"].get("type")
        if interaction_type == "button_reply":
            user_input["button_id"] = message["interactive"]["button_reply"]["id"]
    elif message_type == "text":
        user_input["message_body"] = message["text"]["body"].strip()

    response = asyncio.run(runner.run(whatsapp_agent, [user_input]))
    logging.info(f"Agente respondió: {response}")

# Definición del Blueprint y rutas Flask
webhook_blueprint = Blueprint("webhook", __name__)

@webhook_blueprint.route("/webhook", methods=["GET"])
def webhook_get():
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

@webhook_blueprint.route("/webhook", methods=["POST"])
def webhook_post():
    body = request.get_json()
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

def create_app():
    app = Flask(__name__)
    app.config["ACCESS_TOKEN"] = ACCESS_TOKEN
    app.config["VERIFY_TOKEN"] = VERIFY_TOKEN
    app.config["VERSION"] = VERSION
    app.config["PHONE_NUMBER_ID"] = PHONE_NUMBER_ID

    app.register_blueprint(webhook_blueprint)

    @app.route("/")
    def index():
        return "Servicio activo (versión con @function_tool)", 200

    return app

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
