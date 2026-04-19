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
    logger.info(f"Iniciando processamento: {texto_comando_slack}")

    def send_slack_message(response_url, text=None, blocks=None):
        payload = {"response_type": "in_channel", "replace_original": True}
        if blocks: payload["blocks"] = blocks
        elif text: payload["text"] = text
        try: requests.post(response_url, json=payload, timeout=10)
        except: pass

    try:
        partes = texto_comando_slack.strip().split()
        if len(partes) < 4:
            send_slack_message(response_url, text="Formato incorreto. Use: /comando <tipo> <marca> <ano> <valor>")
            return

        tipo_comando = partes[0].lower()
        marca = partes[1]
        ano = int(partes[2])
        
        # Leitura correta do ID do pedido vs Termo de busca de escola
        if tipo_comando in ["pedido", "itens"]:
            id_pedido_especifico = partes[3]
            filtro_chave = ""
            offset = 0
        else:
            id_pedido_especifico = None
            if partes[-1].isdigit() and len(partes) > 4:
                offset = int(partes[-1])
                filtro_chave = " ".join(partes[3:-1]).strip()
            else:
                offset = 0
                filtro_chave = " ".join(partes[3:]).strip()

        # 1. Autenticação
        token_data = consultar_api_com_retry(URL_TOKEN, {"token": TOKEN_STATICO})
        token = token_data.get("retorno", {}).get("token")
        if not token:
            send_slack_message(response_url, text="Erro de autenticação com a ARCO.")
            return

        # 2. Payload (Sempre Despachavel S)
        payload = {
            "token": token, "Tipo": "pedido", "Marca": marca, "AnoProjeto": ano,
            "DataPedidoInicial": "", "DataPedidoFinal": "", "Despachavel": "S"
        }
        if id_pedido_especifico: 
            payload["Pedido"] = int(id_pedido_especifico)

        # 3. Consulta
        pedidos_brutos = consultar_api_com_retry(URL_PEDIDOS, payload)
        if not isinstance(pedidos_brutos, list):
            send_slack_message(response_url, text="A API não retornou uma lista válida.")
            return

        # 4. Filtro por Escola (Apenas se NÃO for busca por ID específico)
        pedidos_da_escola = []
        if id_pedido_especifico:
            pedidos_da_escola = pedidos_brutos # API já filtrou pelo ID
        else:
            termo_busca = filtro_chave.lower()
            for p in pedidos_brutos:
                chave_p = obter_chave_escola(p).lower()
                nome_escola_p = str(p.get("Escola") or "").lower()
                if termo_busca == chave_p or termo_busca in nome_escola_p:
                    pedidos_da_escola.append(p)

        # 5. Filtros de Status
        pedidos_filtrados = []
        for p in pedidos_da_escola:
            status = str(p.get("StatusPedido") or "").lower()
            if tipo_comando in ["escola_abertos", "busca_chave_abertos", "panorama"]:
                if not any(x in status for x in ["entreg", "realizada", "cancel", "devoluç"]):
                    pedidos_filtrados.append(p)
            elif tipo_comando == "busca_chave_finalizados":
                if any(x in status for x in ["entreg", "realizada", "cancel"]) and "devoluç" not in status:
                    pedidos_filtrados.append(p)
            else:
                pedidos_filtrados.append(p)

        pedidos_filtrados.sort(key=lambda p: int(p.get("idPedido") or 0), reverse=True)

        # Tratamento de lista vazia
        if not pedidos_filtrados:
            msg = "📭 *Nenhum pedido encontrado nesta categoria.*"
            # Se for uma escola, mostra os botões para navegação
            if not id_pedido_especifico:
                val_nav = f"{marca}:::{ano}:::{filtro_chave}"
                botoes = [
                    {"type": "button", "text": {"type": "plain_text", "text": "🏁 Finalizados / Cancelados"}, "value": val_nav, "action_id": "ver_pedidos_finalizados_chave_unica"},
                    {"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "ver_pedidos_abertos_chave_unica"},
                    {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama de Produtos"}, "value": val_nav, "action_id": "ver_panorama_escola"}
                ]
                send_slack_message(response_url, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": msg}}, {"type": "actions", "elements": botoes}])
            else:
                send_slack_message(response_url, text=msg)
            return

        # 6. Montagem da Resposta
        pedido_ref = pedidos_filtrados[0]
        chave_unica = obter_chave_escola(pedido_ref)
        val_nav = f"{marca}:::{ano}:::{chave_unica}"
        
        menu_botoes = [
            {"type": "button", "text": {"type": "plain_text", "text": "🏁 Finalizados / Cancelados"}, "value": val_nav, "action_id": "ver_pedidos_finalizados_chave_unica"},
            {"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "ver_pedidos_abertos_chave_unica"},
            {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama de Produtos"}, "value": val_nav, "action_id": "ver_panorama_escola"}
        ]

        # TELA: ITENS
        if tipo_comando == "itens":
            p = pedidos_filtrados[0]
            blocos = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"🔢 *Número do pedido:* {p.get('idPedido')}\n🏫 *Escola:* {p.get('Escola')}\n🚚 *Status:* {p.get('StatusPedido')}\n📅 *Data Pedido:* {p.get('DataPedido')}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"📦 *Itens (Total: {obter_qtd_total(p)} itens):*\n{formatar_lista_produtos(p.get('Produtos'))}"}},
                {"type": "divider"}, {"type": "actions", "elements": menu_botoes}
            ]
            send_slack_message(response_url, blocks=blocos)
            return

        # TELA: PANORAMA
        if tipo_comando == "panorama":
            blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": f"📊 *Panorama de Produtos em Andamento*\n🏫 *{pedido_ref.get('Escola')}*"}}, {"type": "divider"}]
            for p in pedidos_filtrados:
                txt = f"🔢 *Pedido:* {p.get('idPedido')} | 📦 *Total:* {obter_qtd_total(p)} itens | 🚚 *Status:* {p.get('StatusPedido')}\n{formatar_lista_produtos(p.get('Produtos'))}"
                blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
                blocos.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": f"🔎 Detalhes do {p.get('idPedido')}"}, "value": f"{marca}:::{ano}:::{p.get('idPedido')}", "action_id": "ver_itens_pedido"}]})
                blocos.append({"type": "divider"})
            blocos.append({"type": "actions", "elements": menu_botoes})
            send_slack_message(response_url, blocks=blocos)
            return

        # TELA: LISTAGEM
        resumo = ""
        if "abertos" in tipo_comando:
            counts = {}
            for p in pedidos_filtrados: counts[p.get("StatusPedido")] = counts.get(p.get("StatusPedido"), 0) + 1
            resumo = f"📊 *Resumo de Pedidos em Aberto (Total: {len(pedidos_filtrados)})*\n🏫 *{pedido_ref.get('Escola')}*\n"
            for s, c in counts.items(): resumo += f"• *{c}* - `{s}`\n"
        elif "finalizados" in tipo_comando:
            resumo = f"🏁 *Histórico de Pedidos Finalizados/Cancelados (Total: {len(pedidos_filtrados)})*\n🏫 *{pedido_ref.get('Escola')}*"

        blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": resumo + "\n---"}}] if resumo else []
        
        for p in pedidos_filtrados[offset : offset + 5]:
            status = str(p.get('StatusPedido') or '—')
            id_p = p.get('idPedido')
            txt = f"🔢 *{id_p}* | 📦 {obter_qtd_total(p)} itens | 🚚 {status}"
            if 'entreg' in status.lower() or 'realizada' in status.lower():
                if p.get('DataEntrega'): txt += f"\n✅ *Entregue em:* {p.get('DataEntrega')}"
            elif 'cancel' in status.lower():
                txt += f"\n🚫 *Motivo:* {p.get('MotivoCancelamento') or 'Não informado'}"
            elif p.get('DataExpedicao'):
                txt += f"\n📤 *Expedido em:* {p.get('DataExpedicao')}"

            blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
            blocos.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": f"🔎 Detalhes do {id_p}"}, "value": f"{marca}:::{ano}:::{id_p}", "action_id": "ver_itens_pedido"}]})
            blocos.append({"type": "divider"})

        # Paginação
        if len(pedidos_filtrados) > (offset + 5):
            prox = offset + 5
            aid = "ver_pedidos_abertos_chave_unica" if "abertos" in tipo_comando else ("ver_pedidos_finalizados_chave_unica" if "finalizados" in tipo_comando else "ver_pedidos_chave_unica")
            menu_botoes.append({"type": "button", "text": {"type": "plain_text", "text": "➕ Ver mais"}, "value": f"{marca}:::{ano}:::{chave_unica}:::{prox}", "action_id": aid})

        blocos.append({"type": "actions", "elements": menu_botoes})
        send_slack_message(response_url, blocks=blocos)

    except Exception as e:
        logger.error(f"Erro: {e}", exc_info=True)
        send_slack_message(response_url, text=f"Erro ao processar dados: {str(e)}")

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
    return jsonify({"response_type": "ephemeral", "text": "🛠️ A consultar a ARCO..."}), 200

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    if not verify_slack_signature(request): return "Unauthorized", 401
    payload = json.loads(request.form.get("payload"))
    aid, val = payload["actions"][0]["action_id"], payload["actions"][0]["value"]
    cmds = {"ver_pedidos_chave_unica": "busca_chave", "ver_pedidos_abertos_chave_unica": "busca_chave_abertos", "ver_pedidos_finalizados_chave_unica": "busca_chave_finalizados", "ver_panorama_escola": "panorama", "ver_itens_pedido": "itens"}
    if aid in cmds:
        p = val.split(":::")
        m, a, c, o = p[0], p[1], p[2], (p[3] if len(p) > 3 else "0")
        threading.Thread(target=process_slack_command, args=(payload["response_url"], f"{cmds[aid]} {m} {a} {c} {o}")).start()
    return "", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
