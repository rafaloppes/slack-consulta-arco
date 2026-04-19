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
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") 

if not all([TOKEN_STATICO, URL_TOKEN, URL_PEDIDOS, SLACK_SIGNING_SECRET]):
    logger.critical("Faltam variáveis de ambiente obrigatórias.")
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

def consultar_api_com_retry(url, payload, max_tentativas=3):
    tentativa = 0
    while tentativa < max_tentativas:
        tentativa += 1
        try:
            res = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=45)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            logger.warning(f"Tentativa {tentativa} falhou: {e}")
            if tentativa < max_tentativas: sleep(2)
    raise Exception(f"Falha na API após {max_tentativas} tentativas.")

def obter_chave_escola(pedido):
    codigo = str(pedido.get('CodigoAcesso') or '').strip()
    if codigo: return codigo
    nome = str(pedido.get('Escola') or '').strip()
    cep = str(pedido.get('Cep') or '').strip()
    return f"{nome}||{cep}"

def formatar_lista_produtos(produtos_raw):
    if not produtos_raw: return "• Nenhum item informado"
    return "\n".join([f"• {item.strip()}" for item in produtos_raw.replace('|', ',').split(',') if item.strip()])

def obter_qtd_total(p):
    return p.get('Qtd Produtos') or p.get('QtdProdutos') or '—'

# --- Lógica Principal ---

def process_slack_command(response_url, texto_comando_slack):
    logger.info(f"Processando: {texto_comando_slack}")

    def send_slack_message(response_url, text=None, blocks=None):
        payload = {"response_type": "in_channel", "replace_original": True}
        if blocks: payload["blocks"] = blocks
        elif text: payload["text"] = text
        try: requests.post(response_url, json=payload, timeout=10)
        except: pass

    try:
        partes = texto_comando_slack.strip().split()
        if len(partes) < 3:
            send_slack_message(response_url, text="Formato: /comando <tipo> <marca> [ano] <escola/id>")
            return

        tipo_comando = partes[0].lower()
        marca = partes[1]
        
        # --- LÓGICA DE ANO AUTOMÁTICO ---
        # Pega o ano atual (2026)
        ano_atual = datetime.datetime.now().year
        
        # Verifica se o terceiro item é um ano (número de 4 dígitos entre 2020 e 2030)
        if partes[2].isdigit() and 2020 <= int(partes[2]) <= 2030:
            ano = int(partes[2])
            inicio_busca_idx = 3
        else:
            ano = ano_atual
            inicio_busca_idx = 2 # Se não informou o ano, o filtro começa já no partes[2]

        # Fatiamento inteligente para busca
        if tipo_comando in ["pedido", "itens"]:
            id_pedido_especifico = partes[inicio_busca_idx]
            filtro_chave = ""
            offset = 0
        else:
            id_pedido_especifico = None
            # Paginação no final
            if partes[-1].isdigit() and len(partes) > (inicio_busca_idx + 1):
                offset = int(partes[-1])
                filtro_chave = " ".join(partes[inicio_busca_idx:-1]).strip()
            else:
                offset = 0
                filtro_chave = " ".join(partes[inicio_busca_idx:]).strip()

        # 1. Autenticação
        token_data = consultar_api_com_retry(URL_TOKEN, {"token": TOKEN_STATICO})
        token = token_data.get("retorno", {}).get("token")
        if not token:
            send_slack_message(response_url, text="Erro de autenticação ARCO.")
            return

        # 2. Payload
        payload = {
            "token": token, "Tipo": "pedido", "Marca": marca, "AnoProjeto": ano,
            "DataPedidoInicial": "", "DataPedidoFinal": "", "Despachavel": "S"
        }
        if id_pedido_especifico: payload["Pedido"] = int(id_pedido_especifico)

        # 3. Consulta
        pedidos_brutos = consultar_api_com_retry(URL_PEDIDOS, payload)
        if not isinstance(pedidos_brutos, list):
            send_slack_message(response_url, text=f"Nenhum dado retornado para {marca} em {ano}.")
            return

        # 4. Identificação da Escola
        pedidos_da_escola = []
        if id_pedido_especifico:
            pedidos_da_escola = pedidos_brutos
        else:
            termo = filtro_chave.lower()
            for p in pedidos_brutos:
                if termo == obter_chave_escola(p).lower() or termo in str(p.get("Escola") or "").lower():
                    pedidos_da_escola.append(p)

        # 5. Filtro de Status (Abertos)
        pedidos_filtrados = []
        for p in pedidos_da_escola:
            status = str(p.get("StatusPedido") or "").lower()
            if id_pedido_especifico or tipo_comando not in ["escola_abertos", "busca_chave_abertos", "panorama"]:
                pedidos_filtrados.append(p)
            else:
                if not any(x in status for x in ["entreg", "realizada", "cancel", "devoluç"]):
                    pedidos_filtrados.append(p)

        pedidos_filtrados.sort(key=lambda p: int(p.get("idPedido") or 0), reverse=True)

        if not pedidos_filtrados:
            msg = f"📭 *Nenhum pedido encontrado em {ano}.*"
            if not id_pedido_especifico:
                val_nav = f"{marca}:::{ano}:::{filtro_chave}"
                botoes = [
                    {"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "ver_pedidos_abertos_chave_unica"},
                    {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama de Produtos"}, "value": val_nav, "action_id": "ver_panorama_escola"}
                ]
                send_slack_message(response_url, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": msg}}, {"type": "actions", "elements": botoes}])
            else:
                send_slack_message(response_url, text=msg)
            return

        # 6. Montagem do Menu e Telas
        pedido_ref = pedidos_filtrados[0]
        chave_unica = obter_chave_escola(pedido_ref)
        val_nav = f"{marca}:::{ano}:::{chave_unica}"
        
        menu_botoes = [
            {"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "ver_pedidos_abertos_chave_unica"},
            {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama de Produtos"}, "value": val_nav, "action_id": "ver_panorama_escola"}
        ]

        if tipo_comando == "itens":
            p = pedidos_filtrados[0]
            blocos = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"🔢 *Pedido:* {p.get('idPedido')}\n🏫 *Escola:* {p.get('Escola')}\n🚚 *Status:* {p.get('StatusPedido')}\n📅 *Ano:* {ano}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"📦 *Itens:*\n{formatar_lista_produtos(p.get('Produtos'))}"}},
                {"type": "divider"}, {"type": "actions", "elements": menu_botoes}
            ]
            send_slack_message(response_url, blocks=blocos)
            return

        if tipo_comando == "panorama":
            blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": f"📊 *Panorama de Produtos ({ano})*\n🏫 *{pedido_ref.get('Escola')}*"}}, {"type": "divider"}]
            for p in pedidos_filtrados:
                txt = f"🔢 *Pedido:* {p.get('idPedido')} | 📦 {obter_qtd_total(p)} itens | 🚚 {p.get('StatusPedido')}\n{formatar_lista_produtos(p.get('Produtos'))}"
                blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
                blocos.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "🔎 Detalhes"}, "value": f"{marca}:::{ano}:::{p.get('idPedido')}", "action_id": "ver_itens_pedido"}]})
                blocos.append({"type": "divider"})
            blocos.append({"type": "actions", "elements": menu_botoes})
            send_slack_message(response_url, blocks=blocos)
            return

        # LISTAGEM PADRÃO
        counts = {}
        for p in pedidos_filtrados: counts[p.get("StatusPedido")] = counts.get(p.get("StatusPedido"), 0) + 1
        resumo = f"📊 *Resumo de Pedidos {ano} (Total: {len(pedidos_filtrados)})*\n🏫 *{pedido_ref.get('Escola')}*\n"
        for s, c in counts.items(): resumo += f"• *{c}* - `{s}`\n"

        blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": resumo + "\n---"}}]
        for p in pedidos_filtrados[offset : offset + 5]:
            status = str(p.get('StatusPedido') or '—')
            txt = f"🔢 *{p.get('idPedido')}* | 📦 {obter_qtd_total(p)} itens | 🚚 {status}"
            if p.get('DataExpedicao'): txt += f"\n📤 *Expedido em:* {p.get('DataExpedicao')}"
            
            blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
            blocos.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "🔎 Ver Detalhes"}, "value": f"{marca}:::{ano}:::{p.get('idPedido')}", "action_id": "ver_itens_pedido"}]})
            blocos.append({"type": "divider"})

        if len(pedidos_filtrados) > (offset + 5):
            aid = "ver_pedidos_abertos_chave_unica" if "abertos" in tipo_comando else "ver_pedidos_chave_unica"
            menu_botoes.append({"type": "button", "text": {"type": "plain_text", "text": "➕ Ver mais"}, "value": f"{marca}:::{ano}:::{chave_unica}:::{offset + 5}", "action_id": aid})

        blocos.append({"type": "actions", "elements": menu_botoes})
        send_slack_message(response_url, blocks=blocos)

    except Exception as e:
        logger.error(f"Erro: {e}", exc_info=True)
        send_slack_message(response_url, text="Erro ao processar a consulta.")

# --- Rotas Flask ---

@app.route("/", methods=["GET", "HEAD"])
def index(): return "Bot Ativo", 200

@app.route("/keep-alive", methods=["GET"])
def keep_alive(): return "OK", 200

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
    cmds = {"ver_pedidos_chave_unica": "busca_chave", "ver_pedidos_abertos_chave_unica": "busca_chave_abertos", "ver_panorama_escola": "panorama", "ver_itens_pedido": "itens"}
    if aid in cmds:
        p = val.split(":::")
        m, a, c, o = p[0], p[1], p[2], (p[3] if len(p) > 3 else "0")
        threading.Thread(target=process_slack_command, args=(payload["response_url"], f"{cmds[aid]} {m} {a} {c} {o}")).start()
    return "", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
