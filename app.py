import os
import requests
from flask import Flask, request, jsonify
import threading
import hmac
import hashlib
from time import time
from urllib.parse import parse_qs

app = Flask(__name__)

# Configurações da API ARCO
ARCO_API_KEY = os.getenv("ARCO_API_KEY", "R0VFS0lFLVJBSVpFUy0yMDI0")
ARCO_URL_TOKEN = os.getenv("ARCO_URL_TOKEN", "https://webservice.raizessolucoes.com.br/arco/gerartoken")
ARCO_URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS", "https://webservice.raizessolucoes.com.br/arco/pedidos")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")

def verify_slack_signature(request):
    if not SLACK_SIGNING_SECRET:
        return True
    slack_signature = request.headers.get("X-Slack-Signature")
    slack_timestamp = request.headers.get("X-Slack-Request-Timestamp")
    if not slack_signature or not slack_timestamp:
        return False
    if abs(time() - float(slack_timestamp)) > 60 * 5:
        return False
    body = request.get_data().decode("utf-8")
    sig_basestring = f"v0:{slack_timestamp}:{body}".encode("utf-8")
    computed_sig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        sig_basestring,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed_sig, slack_signature)

def get_arco_token():
    headers = {"Content-Type": "application/json"}
    payload = {"token": ARCO_API_KEY}
    response = requests.post(ARCO_URL_TOKEN, json=payload, headers=headers)
    if response.status_code == 200:
        data = response.json()
        if data.get("retorno", {}).get("statusintegracao") == "SUCESSO":
            return data["retorno"]["token"]
    return None

def fetch_orders(params, token):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    response = requests.post(ARCO_URL_PEDIDOS, json=params, headers=headers)
    if response.status_code == 200:
        return response.json().get("retorno", [])
    return []

def format_order(order):
    return (
        f"🏫 *Escola:* {order.get('escola', '-')}\n"
        f"📦 *Produtos:* {order.get('produtos', '-')}\n"
        f"💲 *Valor:* R$ {order.get('valor', '-')}\n"
        f"🚚 *Status:* {order.get('status', '-')}\n"
        f"📅 *Data Pedido:* {order.get('data_pedido', '-')}\n"
        f"📦 *Expedição:* {order.get('expedicao', '-')}\n"
        f"📧 {order.get('email', '-')}"
    )

def process_slack_command(response_url, text):
    try:
        token = get_arco_token()
        if not token:
            requests.post(response_url, json={"text": "Erro: Falha ao obter token da API ARCO."})
            return

        params = {}
        command_parts = text.strip().split()
        if not command_parts:
            requests.post(response_url, json={"text": "Erro: Comando inválido."})
            return

        command_type = command_parts[0].lower()
        if command_type == "aging":
            params["marca"] = command_parts[1] if len(command_parts) > 1 else ""
            params["ano"] = command_parts[2] if len(command_parts) > 2 else ""
            params["mes"] = command_parts[3] if len(command_parts) > 3 else ""
        elif command_type == "numero":
            params["numero_pedido"] = command_parts[1] if len(command_parts) > 1 else ""
        elif command_type == "expedicao":
            params["data_inicio"] = command_parts[1] if len(command_parts) > 1 else ""
            params["data_fim"] = command_parts[2] if len(command_parts) > 2 else ""
        elif command_type == "escola":
            params["escola"] = " ".join(command_parts[1:]) if len(command_parts) > 1 else ""
        else:
            requests.post(response_url, json={"text": "Erro: Tipo de consulta inválido."})
            return

        orders = fetch_orders(params, token)
        if not orders:
            requests.post(response_url, json={"text": "Nenhum pedido encontrado."})
            return

        response_text = "*📦 Resultados encontrados:*\n\n"
        for order in orders[:5]:
            response_text += format_order(order) + "\n— — — — — — — —\n"
        requests.post(response_url, json={"response_type": "in_channel", "text": response_text})
    except Exception as e:
        requests.post(response_url, json={"text": f"Erro: {str(e)}"})

@app.route("/slack/consulta", methods=["POST"])
def consulta():
    if not verify_slack_signature(request):
        return jsonify({"text": "Assinatura do Slack inválida."}), 403

    form_data = parse_qs(request.get_data().decode("utf-8"))
    text = form_data.get("text", [""])[0]
    response_url = form_data.get("response_url", [""])[0]

    # Iniciar processamento assíncrono
    threading.Thread(target=process_slack_command, args=(response_url, text)).start()

    # Resposta imediata ao Slack
    return jsonify({"text": "Processando sua consulta..."}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
