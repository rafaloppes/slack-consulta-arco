from flask import Flask, request, jsonify
import requests
import os
import threading
import logging
from urllib.parse import parse_qs
import hashlib
import hmac
from hmac import compare_digest
import json

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURAÇÕES ---
TOKEN_STATICO = os.getenv("ARCO_API_KEY")
URL_TOKEN = os.getenv("ARCO_URL_TOKEN")
URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

URL_LOGISTICA = "https://script.google.com/macros/s/AKfycbz-TbuE0FATCGpDumC_RVNiegNFu0J362p7K8GhroRGbBi0f2aHQFPMMyMVv_f4Fh4L/exec"
TOKEN_LOGISTICA = "ARCO_LOG_2026"

def obter_logistica(id_pedido):
    try:
        url = f"{URL_LOGISTICA}?id={id_pedido}&token={TOKEN_LOGISTICA}"
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            dados = res.json()
            return dados if "erro" not in dados else None
    except Exception as e:
        logger.error(f"Erro na ponte: {e}")
    return None

def consultar_arco(url, payload):
    try:
        res = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=40)
        return res.json() if res.status_code == 200 else None
    except: return None

def process_command(response_url, text):
    try:
        partes = text.strip().split()
        if len(partes) < 2: return
        tipo, marca = partes[0].lower(), partes[1]
        ano, idx = (int(partes[2]), 3) if len(partes) > 2 and partes[2].isdigit() and len(partes[2]) == 4 else (2026, 2)
        
        id_pedido = partes[idx] if tipo in ["pedido", "itens"] else None
        
        # 1. Busca ARCO
        tk_res = consultar_arco(URL_TOKEN, {"token": TOKEN_STATICO})
        token = tk_res.get("retorno", {}).get("token")
        p_arco = consultar_arco(URL_PEDIDOS, {"token": token, "Tipo": "pedido", "Marca": marca, "AnoProjeto": ano, "Pedido": int(id_pedido), "Despachavel": "S"})
        
        if not p_arco or not isinstance(p_arco, list):
            requests.post(response_url, json={"text": "📭 Pedido não encontrado na ARCO."})
            return

        p = p_arco[0]
        # 2. Busca Logística (Integrada)
        log = obter_logistica(p.get('idPedido'))
        
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"🔢 *Pedido: {p.get('idPedido')}* | 🏫 {p.get('Escola')}\n🚚 *Status:* {p.get('StatusPedido')}"}}]

        if log:
            # Pegando as chaves dinâmicas da planilha
            linhas = [
                f"🚛 *Transportadora:* {log.get('transportador', '—')}",
                f"📄 *Nota Fiscal:* {log.get('numero_nota', '—')}",
                f"📅 *Previsão:* {log.get('prev_inicial', '—')}"
            ]
            
            # Rastreio (Coluna R: COD RASTREIO -> cod_rastreio)
            rastreio = str(log.get('cod_rastreio', '')).strip()
            if rastreio and rastreio not in ["-", ""]:
                linhas.append(f"📦 *Código de Rastreio:* {rastreio}")
            
            # Data de Entrega (Coluna Q: DATA ENTREGA -> data_entrega)
            dt_ent = str(log.get('data_entrega', '')).strip()
            if dt_ent and dt_ent not in ["-", ""]:
                linhas.append(f"📍 *Data de Entrega:* {dt_ent}")

            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Dados de Entrega:*\n" + "\n".join(linhas)}})

        prods = "\n".join([f"• {i.strip()}" for i in str(p.get('Produtos')).replace('|', ',').split(',') if i.strip()])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"📦 *Produtos:*\n{prods}"}})
        
        # Botões
        val_nav = f"{marca}:::{ano}:::{p.get('CodigoAcesso') or p.get('Escola')}"
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "nav_abertos"},
            {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama da Escola"}, "value": val_nav, "action_id": "nav_panorama"}
        ]})

        requests.post(response_url, json={"blocks": blocks, "replace_original": True})

    except Exception as e:
        logger.error(f"Erro: {e}")

@app.route("/slack/commands", methods=["POST"])
def slack_command():
    form = parse_qs(request.get_data().decode("utf-8"))
    threading.Thread(target=process_command, args=(form["response_url"][0], form["text"][0])).start()
    return jsonify({"response_type": "ephemeral", "text": "🛠️ Consultando ARCO..."}), 200

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    payload = json.loads(request.form.get("payload"))
    aid, val = payload["actions"][0]["action_id"], payload["actions"][0]["value"]
    p = val.split(":::")
    cmd_map = {"nav_detalhes": "itens", "nav_abertos": "escola_abertos", "nav_panorama": "panorama"}
    if aid in cmd_map:
        threading.Thread(target=process_command, args=(payload["response_url"], f"{cmd_map[aid]} {p[0]} {p[1]} {p[2]}")).start()
    return "", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
