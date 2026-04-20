from flask import Flask, request, jsonify
import requests
from datetime import datetime, date, timedelta
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

TOKEN_STATICO = os.getenv("ARCO_API_KEY")
URL_TOKEN = os.getenv("ARCO_URL_TOKEN")
URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
URL_LOGISTICA = "https://script.google.com/macros/s/AKfycbz-TbuE0FATCGpDumC_RVNiegNFu0J362p7K8GhroRGbBi0f2aHQFPMMyMVv_f4Fh4L/exec"
TOKEN_LOGISTICA = "ARCO_LOG_2026"

def formatar_data_br(data_str):
    if not data_str or str(data_str).strip() in ["-", "None", ""]: return None
    try:
        if str(data_str).replace('.','',1).isdigit():
            dt = datetime(1899, 12, 30) + timedelta(days=int(float(data_str)))
            return dt.strftime('%d/%m/%Y')
        dt = datetime.fromisoformat(str(data_str).replace('Z', '+00:00'))
        return dt.strftime('%d/%m/%Y')
    except:
        try:
            dt = datetime.strptime(str(data_str)[:10], '%Y-%m-%d')
            return dt.strftime('%d/%m/%Y')
        except: return str(data_str)

def converter_para_objeto_data(data_str):
    if not data_str or str(data_str).strip() in ["-", "None", ""]: return None
    try:
        if str(data_str).replace('.','',1).isdigit():
            return (datetime(1899, 12, 30) + timedelta(days=int(float(data_str)))).date()
        return datetime.fromisoformat(str(data_str).replace('Z', '+00:00')).date()
    except:
        try: return datetime.strptime(str(data_str)[:10], '%Y-%m-%d').date()
        except: return None

def consultar_rastreio_correios(codigo):
    if not codigo or len(codigo) < 8: return None
    try:
        url = f"https://api.linketrack.com/track/json?user=teste&token=1abcd02192ee382fe05520fd1120cdc51efad2e8&codigo={codigo}"
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            eventos = res.json().get('eventos', [])
            if eventos:
                u = eventos[0]
                return f"{u.get('status')} em {u.get('data')} ({u.get('hora')})"
    except: return "⚠️ Status indisponível no momento, use o link acima."
    return None

def obter_logistica(id_pedido):
    try:
        res = requests.get(f"{URL_LOGISTICA}?id={id_pedido}&token={TOKEN_LOGISTICA}", timeout=15)
        return res.json() if res.status_code == 200 and "erro" not in res.json() else None
    except: return None

def consultar_arco(url, payload):
    try:
        res = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=40)
        return res.json() if res.status_code == 200 else None
    except: return None

def process_command(response_url, text):
    try:
        partes = text.strip().split()
        if len(partes) < 2: return
        tipo, marca_input = partes[0].lower(), partes[1].lower()
        ano, idx = (int(partes[2]), 3) if len(partes) > 2 and partes[2].isdigit() and len(partes[2]) == 4 else (2026, 2)
        id_pedido = partes[idx] if len(partes) > idx else None

        marcas_api = ['nave', 'geekie']
        log = obter_logistica(id_pedido)

        if marca_input in marcas_api:
            tk_res = consultar_arco(URL_TOKEN, {"token": TOKEN_STATICO})
            token = tk_res.get("retorno", {}).get("token")
            p_list = consultar_arco(URL_PEDIDOS, {"token": token, "Tipo": "pedido", "Marca": marca_input, "AnoProjeto": ano, "Pedido": int(id_pedido), "Despachavel": "S"})
            if not p_list:
                requests.post(response_url, json={"text": f"📭 Pedido {id_pedido} não localizado."})
                return
            p = p_list[0]
            pedido_resumo = {"id": p.get('idPedido'), "marca": marca_input.upper(), "escola": p.get('Escola'), "status": p.get('StatusPedido'), "produtos": "\n".join([f"• {i.strip()}" for i in str(p.get('Produtos', '')).replace('|', ',').split(',') if i.strip()]), "origem_api": True, "codigo_acesso": p.get('CodigoAcesso')}
        else:
            if not log or str(log.get('marca', '')).lower() != marca_input:
                requests.post(response_url, json={"text": f"📭 Pedido não localizado na Logística."})
                return
            pedido_resumo = {"id": id_pedido, "marca": str(log.get('marca', marca_input)).upper(), "escola": log.get('cliente', 'Escola não identificada'), "status": "Entregue" if formatar_data_br(log.get('data_entrega')) else "Em trânsito", "produtos": "_Itens de logística (Detalhamento disponível em breve)_", "origem_api": False}

        header = f"🔢 *Pedido: {pedido_resumo['id']}* | 🏷️ *Marca:* {pedido_resumo['marca']}\n🏫 {pedido_resumo['escola']}\n🚚 *Status:* {pedido_resumo['status']}"
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]

        if log:
            hoje = date.today()
            transp = str(log.get('transportador', '')).upper()
            linhas = [f"🚛 *Transportadora:* {log.get('transportador', '—')}", f"📄 *Nota Fiscal:* {log.get('numero_nota', '—')}"]
            
            dt_ini_fmt = formatar_data_br(log.get('prev_inicial'))
            dt_ent_fmt = formatar_data_br(log.get('data_entrega'))
            dt_ini_obj = converter_para_objeto_data(log.get('prev_inicial'))
            
            if dt_ini_fmt: linhas.append(f"📅 *Previsão Inicial:* {dt_ini_fmt}")
            if dt_ini_obj and dt_ini_obj < hoje and not dt_ent_fmt:
                obs = str(log.get('obs', '')).strip()
                if obs and obs not in ["-", ""]: linhas.append(f"⚠️ *Ocorrência:* {obs}")
                linhas.append(f"📍 *Nova Previsão:* {formatar_data_br(log.get('prev_atualizada')) or 'Aguardando nova previsão'}")

            rastreio = str(log.get('cod_rastreio', '')).strip()
            if "CORREIOS" in transp:
                if not rastreio or rastreio == "-":
                    linhas.append("📦 *Rastreio:* ainda não disponível, procure o time de transportes")
                else:
                    link = f"https://www.linketrack.com/track?codigo={rastreio}"
                    linhas.append(f"📦 *Rastreio:* <{link}|{rastreio}> (clique para abrir)")
                    st_real = consultar_rastreio_correios(rastreio)
                    if st_real: linhas.append(f"🔍 *Status Real:* {st_real}")
            elif rastreio and rastreio != "-":
                linhas.append(f"📦 *Rastreio:* {rastreio}")

            if dt_ent_fmt: linhas.append(f"✅ *Entregue em:* {dt_ent_fmt}")
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Dados de Entrega:*\n" + "\n".join(linhas)}})

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"📦 *Produtos:*\n{pedido_resumo['produtos']}"}})
        if pedido_resumo.get('origem_api'):
            val_nav = f"{marca_input}:::{ano}:::{pedido_resumo.get('codigo_acesso') or pedido_resumo['escola']}"
            blocks.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "nav_abertos"}, {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama da Escola"}, "value": val_nav, "action_id": "nav_panorama"}]})

        requests.post(response_url, json={"blocks": blocks, "replace_original": True})
    except Exception as e:
        logger.error(f"Erro: {e}")

def verify_slack_signature(request):
    sig, ts = request.headers.get("X-Slack-Signature", ""), request.headers.get("X-Slack-Request-Timestamp", "")
    if not sig or not ts: return False
    basestring = f"v0:{ts}:{request.get_data().decode('utf-8')}".encode('utf-8')
    computed = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode('utf-8'), basestring, hashlib.sha256).hexdigest()
    return compare_digest(computed, sig)

@app.route("/slack/commands", methods=["POST"])
def slack_command():
    if not verify_slack_signature(request): return "Unauthorized", 401
    form = parse_qs(request.get_data().decode("utf-8"))
    threading.Thread(target=process_command, args=(form["response_url"][0], form["text"][0])).start()
    return jsonify({"response_type": "ephemeral", "text": "🛠️ Consultando sistemas..."}), 200

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    if not verify_slack_signature(request): return "Unauthorized", 401
    payload = json.loads(request.form.get("payload"))
    aid, val = payload["actions"][0]["action_id"], payload["actions"][0]["value"]
    p = val.split(":::")
    cmd_map = {"nav_detalhes": "itens", "nav_abertos": "escola_abertos", "nav_panorama": "panorama"}
    if aid in cmd_map:
        threading.Thread(target=process_command, args=(payload["response_url"], f"{cmd_map[aid]} {p[0]} {p[1]} {p[2]}")).start()
    return "", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
