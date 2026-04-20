from flask import Flask, request, jsonify
import requests
from datetime import datetime, date
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

# --- CONFIGURAÇÕES DE AMBIENTE ---
# Certifique-se de que estas variáveis estão configuradas no painel do Render
TOKEN_STATICO = os.getenv("ARCO_API_KEY")
URL_TOKEN = os.getenv("ARCO_URL_TOKEN")
URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# URL DA SUA PONTE (GMAIL PESSOAL)
URL_LOGISTICA = "https://script.google.com/macros/s/AKfycbz-TbuE0FATCGpDumC_RVNiegNFu0J362p7K8GhroRGbBi0f2aHQFPMMyMVv_f4Fh4L/exec"
TOKEN_LOGISTICA = "ARCO_LOG_2026"

# --- AUXILIARES DE DATA ---

def formatar_data_br(data_str):
    """Transforma qualquer formato de data para DD/MM/AAAA"""
    if not data_str or str(data_str).strip() in ["-", "None", ""]: return None
    try:
        # Tenta tratar formatos ISO (2026-04-20T03:00:00Z)
        dt = datetime.fromisoformat(str(data_str).replace('Z', '+00:00'))
        return dt.strftime('%d/%m/%Y')
    except:
        try:
            # Fallback para YYYY-MM-DD
            dt = datetime.strptime(str(data_str)[:10], '%Y-%m-%d')
            return dt.strftime('%d/%m/%Y')
        except:
            return str(data_str)

def converter_para_objeto_data(data_str):
    """Converte string para objeto de data para cálculos e comparações"""
    if not data_str or str(data_str).strip() in ["-", "None", ""]: return None
    try:
        dt = datetime.fromisoformat(str(data_str).replace('Z', '+00:00'))
        return dt.date()
    except:
        try:
            return datetime.strptime(str(data_str)[:10], '%Y-%m-%d').date()
        except:
            return None

# --- FUNÇÕES DE COMUNICAÇÃO ---

def obter_logistica(id_pedido):
    """Busca dados na planilha via ponte dinâmica (GMAIL)"""
    try:
        url = f"{URL_LOGISTICA}?id={id_pedido}&token={TOKEN_LOGISTICA}"
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            dados = res.json()
            return dados if "erro" not in dados else None
    except Exception as e:
        logger.error(f"Erro ao acessar logística: {e}")
    return None

def consultar_arco(url, payload):
    """Consulta padrão na API da ARCO"""
    try:
        res = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=40)
        return res.json() if res.status_code == 200 else None
    except: return None

# --- PROCESSAMENTO DO COMANDO ---

def process_command(response_url, text):
    try:
        partes = text.strip().split()
        if len(partes) < 2: return
        
        tipo, marca = partes[0].lower(), partes[1]
        # Detecta ano se houver (ex: 2026), senão assume o atual
        ano, idx = (int(partes[2]), 3) if len(partes) > 2 and partes[2].isdigit() and len(partes[2]) == 4 else (2026, 2)
        
        id_pedido = partes[idx] if tipo in ["pedido", "itens"] else None

        # 1. Autenticação e Busca na ARCO
        tk_res = consultar_arco(URL_TOKEN, {"token": TOKEN_STATICO})
        arco_token = tk_res.get("retorno", {}).get("token")
        
        payload_arco = {"token": arco_token, "Tipo": "pedido", "Marca": marca, "AnoProjeto": ano, "Pedido": int(id_pedido), "Despachavel": "S"}
        resultado_arco = consultar_arco(URL_PEDIDOS, payload_arco)
        
        if not resultado_arco or not isinstance(resultado_arco, list):
            requests.post(response_url, json={"text": f"📭 Pedido {id_pedido} não localizado na ARCO."})
            return

        p = resultado_arco[0]
        
        # 2. Busca e Racional de Logística
        log = obter_logistica(p.get('idPedido'))
        
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"🔢 *Pedido: {p.get('idPedido')}* | 🏫 {p.get('Escola')}\n🚚 *Status:* {p.get('StatusPedido')}"}}]

        if log:
            hoje = date.today()
            linhas_entrega = [
                f"🚛 *Transportadora:* {log.get('transportador', '—')}",
                f"📄 *Nota Fiscal:* {log.get('numero_nota', '—')}"
            ]
            
            # Captura dados brutos da planilha (mapeados por nome)
            data_ini_raw = log.get('prev_inicial')
            data_atu_raw = log.get('prev_atualizada')
            data_ent_raw = log.get('data_entrega')
            obs = str(log.get('obs', '')).strip()

            # Exibição da Previsão Inicial
            dt_ini_fmt = formatar_data_br(data_ini_raw)
            if dt_ini_fmt:
                linhas_entrega.append(f"📅 *Previsão Inicial:* {dt_ini_fmt}")

            # Lógica de Atraso e Ocorrências
            dt_ini_obj = converter_para_objeto_data(data_ini_raw)
            dt_ent_fmt = formatar_data_br(data_ent_raw)

            # Se venceu a inicial e ainda NÃO foi entregue
            if dt_ini_obj and dt_ini_obj < hoje and not dt_ent_fmt:
                # Mostra Ocorrência (OBS) se existir
                if obs and obs not in ["-", ""]:
                    linhas_entrega.append(f"⚠️ *Ocorrência de entrega:* {obs}")
                
                # Gerencia a Nova Previsão
                dt_atu_fmt = formatar_data_br(data_atu_raw)
                if dt_atu_fmt:
                    linhas_entrega.append(f"📍 *Nova Previsão:* {dt_atu_fmt}")
                else:
                    # Se não tem nova data (com ou sem OBS), sinaliza pendência
                    linhas_entrega.append(f"⏳ *Status:* Aguardando nova previsão de entrega")

            # Código de Rastreio (Coluna R)
            rastreio = str(log.get('cod_rastreio', '')).strip()
            if rastreio and rastreio != "-":
                linhas_entrega.append(f"📦 *Código de Rastreio:* {rastreio}")

            # Data de Entrega Final
            if dt_ent_fmt:
                linhas_entrega.append(f"✅ *Data de Entrega:* {dt_ent_fmt}")

            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Dados de Entrega:*\n" + "\n".join(linhas_entrega)}})

        # 3. Produtos e Navegação
        prods = "\n".join([f"• {i.strip()}" for i in str(p.get('Produtos', '')).replace('|', ',').split(',') if i.strip()])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"📦 *Produtos:*\n{prods}"}})
        
        val_nav = f"{marca}:::{ano}:::{p.get('CodigoAcesso') or p.get('Escola')}"
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "nav_abertos"},
            {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama da Escola"}, "value": val_nav, "action_id": "nav_panorama"}
        ]})

        requests.post(response_url, json={"blocks": blocks, "replace_original": True})

    except Exception as e:
        logger.error(f"Erro crítico: {e}")
        requests.post(response_url, json={"text": "Ocorreu um erro ao processar a consulta."})

# --- SEGURANÇA SLACK ---

def verify_slack_signature(request):
    sig = request.headers.get("X-Slack-Signature", "")
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    if not sig or not ts: return False
    body = request.get_data().decode("utf-8")
    basestring = f"v0:{ts}:{body}".encode("utf-8")
    computed = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return compare_digest(computed, sig)

# --- ROTAS ---

@app.route("/slack/commands", methods=["POST"])
def slack_command():
    if not verify_slack_signature(request): return "Unauthorized", 401
    form = parse_qs(request.get_data().decode("utf-8"))
    threading.Thread(target=process_command, args=(form["response_url"][0], form["text"][0])).start()
    return jsonify({"response_type": "ephemeral", "text": "🛠️ Consultando ARCO..."}), 200

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
