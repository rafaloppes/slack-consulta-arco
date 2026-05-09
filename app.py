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

# --- CONFIGURAÇÕES ---
TOKEN_STATICO = os.getenv("ARCO_API_KEY")
URL_TOKEN = os.getenv("ARCO_URL_TOKEN")
URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
URL_LOGISTICA = "https://script.google.com/macros/s/AKfycbz-TbuE0FATCGpDumC_RVNiegNFu0J362p7K8GhroRGbBi0f2aHQFPMMyMVv_f4Fh4L/exec"
TOKEN_LOGISTICA = "ARCO_LOG_2026"

# --- AUXILIARES ---

def formatar_data_br(data_str):
    if not data_str or str(data_str).strip() in ["-", "None", ""]: return None
    try:
        if str(data_str).replace('.','',1).isdigit():
            dt = datetime(1899, 12, 30) + timedelta(days=int(float(data_str)))
            return dt.strftime('%d/%m/%Y')
        dt = datetime.fromisoformat(str(data_str).replace('Z', '+00:00'))
        return dt.strftime('%d/%m/%Y')
    except:
        try: return datetime.strptime(str(data_str)[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
        except: return str(data_str)

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
    except: return "⚠️ Status indisponível no momento."
    return None

def consultar_arco(url, payload):
    try:
        res = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=40)
        return res.json() if res.status_code == 200 else None
    except: return None

# --- PROCESSAMENTO PRINCIPAL ---

def process_command(response_url, text):
    try:
        partes = text.strip().split()
        if len(partes) < 2: return
        
        # O primeiro termo pode ser "pedido", "escola_abertos" ou "panorama"
        cmd_tipo = partes[0].lower()
        marca_input = partes[1].lower()
        
        # Lógica de Ano (Assume 2026 se não houver um ano de 4 dígitos)
        if len(partes) > 2 and partes[2].isdigit() and len(partes[2]) == 4:
            ano = int(partes[2])
            idx_inicio = 3
        else:
            ano = 2026
            idx_inicio = 2
            
        # Pega o restante do texto (ID do pedido ou nome da escola com espaços)
        termo_busca = " ".join(partes[idx_inicio:])

        if not termo_busca:
            requests.post(response_url, json={"text": "⚠️ Informe o pedido ou nome da escola."})
            return

        marcas_api = ['nave', 'geekie']
        
        if marca_input in marcas_api:
            tk_res = consultar_arco(URL_TOKEN, {"token": TOKEN_STATICO})
            token = tk_res.get("retorno", {}).get("token")
            
            # Define se a API deve buscar por "pedido", "escola" ou "panorama"
            tipo_api = "pedido" if cmd_tipo == "pedido" else ("escola" if "aberto" in cmd_tipo else "panorama")
            
            payload = {"token": token, "Tipo": tipo_api, "Marca": marca_input, "AnoProjeto": ano, "Despachavel": "S"}
            
            if tipo_api == "pedido":
                payload["Pedido"] = int(termo_busca)
            else:
                payload["Escola"] = termo_busca

            dados_arco = consultar_arco(URL_PEDIDOS, payload)

            if not dados_arco:
                requests.post(response_url, json={"text": f"📭 Nenhuma informação encontrada para '{termo_busca}'."})
                return

            # SE FOR PANORAMA OU ABERTOS: Retorna uma lista simples
            if tipo_api != "pedido":
                titulo = "📊 PANORAMA" if tipo_api == "panorama" else "⏳ PEDIDOS EM ABERTO"
                msg = f"*{titulo} - {marca_input.upper()}*\n🏫 *Escola:* {termo_busca}\n" + "---" * 5 + "\n"
                for p in dados_arco[:15]:
                    msg += f"• *Pedido {p.get('idPedido')}*: {p.get('StatusPedido')}\n  _Produtos: {str(p.get('Produtos',''))[:80]}..._\n"
                requests.post(response_url, json={"text": msg})
                return

            # SE FOR PEDIDO ÚNICO: Segue o fluxo detalhado com Logística
            p = dados_arco[0]
            log = requests.get(f"{URL_LOGISTICA}?id={termo_busca}&token={TOKEN_LOGISTICA}").json()
            log = log if "erro" not in log else None
            
            header = f"🔢 *Pedido: {p.get('idPedido')}* | 🏷️ *Marca:* {marca_input.upper()}\n🏫 {p.get('Escola')}\n🚚 *Status:* {p.get('StatusPedido')}"
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]

            if log:
                transp = str(log.get('transportador', '')).upper()
                rastreio = str(log.get('cod_rastreio', '')).strip()
                linhas = [f"🚛 *Transportadora:* {log.get('transportador', '—')}", f"📄 *Nota Fiscal:* {log.get('numero_nota', '—')}"]
                
                if "CORREIOS" in transp and rastreio != "-":
                    link = f"https://www.linketrack.com/track?codigo={rastreio}"
                    linhas.append(f"📦 *Rastreio:* <{link}|{rastreio}>")
                    st = consultar_rastreio_correios(rastreio)
                    if st: linhas.append(f"🔍 *Status Real:* {st}")
                elif rastreio != "-":
                    linhas.append(f"📦 *Rastreio:* {rastreio}")

                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Logística:*\n" + "\n".join(linhas)}})

            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"📦 *Produtos:*\n{p.get('Produtos','')}"}})
            
            # BOTÕES (Corrigidos para passar a marca correta e o nome/código)
            val_nav = f"{marca_input}:::{ano}:::{p.get('CodigoAcesso') or p.get('Escola')}"
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "nav_abertos"},
                {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama da Escola"}, "value": val_nav, "action_id": "nav_panorama"}
            ]})
            
            requests.post(response_url, json={"blocks": blocks, "replace_original": True})
        else:
            # Lógica para outras marcas da Planilha se mantém...
            requests.post(response_url, json={"text": f"✅ Pedido {termo_busca} localizado via Logística."})

    except Exception as e:
        logger.error(f"Erro: {e}")

# --- SLACK ROTAS ---

def verify_slack_signature(request):
    sig = request.headers.get("X-Slack-Signature", "")
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    if not sig or not ts: return False
    body = request.get_data().decode("utf-8")
    basestring = f"v0:{ts}:{body}".encode("utf-8")
    computed = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
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
    aid = payload["actions"][0]["action_id"]
    val = payload["actions"][0]["value"] # Formato: marca:::ano:::escola
    p = val.split(":::")
    
    # Mapeamento para os comandos que a função process_command entende
    cmd_map = {"nav_abertos": "escola_abertos", "nav_panorama": "panorama"}
    
    if aid in cmd_map:
        # Reconstrói o texto do comando: "panorama geekie 2026 NOME DA ESCOLA"
        comando_refeito = f"{cmd_map[aid]} {p[0]} {p[1]} {p[2]}"
        threading.Thread(target=process_command, args=(payload["response_url"], comando_refeito)).start()
    
    return "", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
