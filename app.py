from flask import Flask, request, jsonify
import requests
import datetime
import os
import threading
import logging
from urllib.parse import parse_qs
import hashlib
import hmac
from hmac import compare_digest
from time import time, sleep
import json
import random
import sys

app = Flask(__name__)

# --- Configurações ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN_STATICO = os.getenv("ARCO_API_KEY")
URL_TOKEN = os.getenv("ARCO_URL_TOKEN")
URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# URL do teu Apps Script (Certifica-te de que está correta no teu Render)
URL_LOGISTICA_PLANILHA = "https://script.google.com/a/macros/arcoeducacao.com.br/s/AKfycbwQm7ag5uiZTe7laz-EPWK_SaxipYgRFeWmMKdz9fl.../exec"

if not all([TOKEN_STATICO, URL_TOKEN, URL_PEDIDOS, SLACK_SIGNING_SECRET]):
    logger.critical("Faltam variáveis de ambiente.")
    sys.exit(1)

# --- Funções Auxiliares ---

def verify_slack_signature(request):
    if not SLACK_SIGNING_SECRET: return False
    slack_signature = request.headers.get("X-Slack-Signature")
    slack_timestamp = request.headers.get("X-Slack-Request-Timestamp")
    if not slack_signature or not slack_timestamp: return False
    if abs(time() - float(slack_timestamp)) > 60 * 5: return False
    body = request.get_data().decode("utf-8")
    sig_basestring = f"v0:{slack_timestamp}:{body}".encode("utf-8")
    computed_sig = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode("utf-8"), sig_basestring, hashlib.sha256).hexdigest()
    return compare_digest(computed_sig, slack_signature)

def consultar_api_arco(url, payload):
    try:
        res = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=45)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        logger.error(f"Erro ARCO: {e}")
        return None

def obter_logistica_planilha(id_pedido):
    try:
        url_final = f"{URL_LOGISTICA_PLANILHA}?id={id_pedido}"
        res = requests.get(url_final, timeout=10)
        res.raise_for_status()
        dados = res.json()
        return dados if dados and "erro" not in dados else None
    except:
        return None

def obter_chave_escola(pedido):
    codigo = str(pedido.get('CodigoAcesso') or '').strip()
    if codigo: return codigo
    nome = str(pedido.get('Escola') or '').strip()
    cep = str(pedido.get('Cep') or '').strip()
    return f"{nome}||{cep}"

def formatar_lista_produtos(produtos_raw):
    if not produtos_raw: return "• Nenhum item informado"
    return "\n".join([f"• {item.strip()}" for item in produtos_raw.replace('|', ',').split(',') if item.strip()])

# --- Lógica Principal ---

def process_slack_command(response_url, texto_comando_slack):
    def send_slack_message(response_url, text=None, blocks=None):
        payload = {"response_type": "in_channel", "replace_original": True}
        if blocks: payload["blocks"] = blocks
        elif text: payload["text"] = text
        requests.post(response_url, json=payload, timeout=10)

    try:
        partes = texto_comando_slack.strip().split()
        tipo_comando = partes[0].lower()
        marca = partes[1]
        
        # Ano automático
        ano = 2026
        inicio_idx = 2
        if len(partes) > 2 and partes[2].isdigit() and 2020 <= int(partes[2]) <= 2030:
            ano = int(partes[2])
            inicio_idx = 3

        id_pedido_especifico = partes[inicio_idx] if tipo_comando in ["pedido", "itens"] else None
        filtro_chave = " ".join(partes[inicio_idx:]).strip() if not id_pedido_especifico else ""
        offset = int(partes[-1]) if partes[-1].isdigit() and len(partes) > (inicio_idx + 1) else 0

        # 1. Busca ARCO
        token_data = consultar_api_arco(URL_TOKEN, {"token": TOKEN_STATICO})
        token = token_data.get("retorno", {}).get("token")
        
        payload_arco = {"token": token, "Tipo": "pedido", "Marca": marca, "AnoProjeto": ano, "Despachavel": "S"}
        if id_pedido_especifico: payload_arco["Pedido"] = int(id_pedido_especifico)
        
        pedidos_brutos = consultar_api_arco(URL_PEDIDOS, payload_arco)
        if not isinstance(pedidos_brutos, list) or not pedidos_brutos:
            send_slack_message(response_url, text="📭 Pedido ou escola não encontrados.")
            return

        # 2. Filtro
        pedidos_final = []
        if id_pedido_especifico:
            pedidos_final = pedidos_brutos
        else:
            termo = filtro_chave.lower()
            for p in pedidos_brutos:
                if termo == obter_chave_escola(p).lower() or termo in str(p.get("Escola") or "").lower():
                    status = str(p.get("StatusPedido") or "").lower()
                    if not any(x in status for x in ["entreg", "realizada", "cancel"]):
                        pedidos_final.append(p)

        if not pedidos_final:
            send_slack_message(response_url, text="📭 Nenhum pedido pendente encontrado.")
            return

        # 3. Preparação do Menu de Botões (Sempre visível)
        p_ref = pedidos_final[0]
        chave_escola = obter_chave_escola(p_ref)
        val_nav = f"{marca}:::{ano}:::{chave_escola}"
        menu_botoes = [
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "ver_pedidos_abertos_chave_unica"},
                {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama de Produtos"}, "value": val_nav, "action_id": "ver_panorama_escola"}
            ]}
        ]

        # TELA DE DETALHE
        if id_pedido_especifico or tipo_comando == "itens":
            p = pedidos_final[0]
            logistica = obter_logistica_planilha(p.get('idPedido'))
            
            txt_topo = f"🔢 *Pedido:* {p.get('idPedido')} | 🏫 {p.get('Escola')}\n🚚 *Status ARCO:* {p.get('StatusPedido')}"
            blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": txt_topo}}]

            if logistica:
                info_planilha = (f"🚛 *Transportador:* {logistica.get('transportador', '—')}\n"
                                 f"📄 *Nota Fiscal:* {logistica.get('numero_nota', '—')}\n"
                                 f"📅 *Previsão:* {logistica.get('prev_inicial', '—')}")
                blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*📦 Info Planilha:*\n{info_planilha}"}})

            blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": f"📦 *Produtos:*\n{formatar_lista_produtos(p.get('Produtos'))}"}})
            blocos.extend(menu_botoes)
            send_slack_message(response_url, blocks=blocos)
            return

        # TELA DE PANORAMA
        if tipo_comando == "panorama":
            blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": f"📊 *Panorama de Itens*\n🏫 *{p_ref.get('Escola')}*"}}, {"type": "divider"}]
            for item in pedidos_final:
                txt = f"🔢 *Pedido:* {item.get('idPedido')} | 🚚 {item.get('StatusPedido')}\n{formatar_lista_produtos(item.get('Produtos'))}"
                blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": txt},
                               "accessory": {"type": "button", "text": {"type": "plain_text", "text": "🔎 Detalhes"}, "value": f"{marca}:::{ano}:::{item.get('idPedido')}", "action_id": "ver_itens_pedido"}})
            blocos.extend(menu_botoes)
            send_slack_message(response_url, blocks=blocos)
            return

        # TELA DE LISTAGEM
        blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": f"📋 *Pedidos Pendentes - {p_ref.get('Escola')}*"}}]
        for item in pedidos_final[offset : offset + 5]:
            blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": f"🔢 *{item.get('idPedido')}* | 🚚 {item.get('StatusPedido')}"},
                           "accessory": {"type": "button", "text": {"type": "plain_text", "text": "🔎 Detalhes"}, "value": f"{marca}:::{ano}:::{item.get('idPedido')}", "action_id": "ver_itens_pedido"}})
        
        blocos.extend(menu_botoes)
        send_slack_message(response_url, blocks=blocos)

    except Exception as e:
        logger.error(f"Erro: {e}")
        send_slack_message(response_url, text="Erro ao processar consulta.")

# --- Rotas Flask ---
@app.route("/slack/commands", methods=["POST"])
def slack_command():
    if not verify_slack_signature(request): return "Unauthorized", 401
    form = parse_qs(request.get_data().decode("utf-8"))
    threading.Thread(target=process_slack_command, args=(form.get("response_url", [""])[0], form.get("text", [""])[0])).start()
    return jsonify({"response_type": "ephemeral", "text": "🛠️ Consultando ARCO..."}), 200

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    if not verify_slack_signature(request): return "Unauthorized", 401
    payload = json.loads(request.form.get("payload"))
    aid, val = payload["actions"][0]["action_id"], payload["actions"][0]["value"]
    if aid in ["ver_itens_pedido", "ver_pedidos_abertos_chave_unica", "ver_panorama_escola"]:
        p = val.split(":::")
        cmd_map = {"ver_itens_pedido": "itens", "ver_pedidos_abertos_chave_unica": "escola_abertos", "ver_panorama_escola": "panorama"}
        threading.Thread(target=process_slack_command, args=(payload["response_url"], f"{cmd_map[aid]} {p[0]} {p[1]} {p[2]}")).start()
    return "", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
