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

# --- Verificação na Inicialização ---
if not all([TOKEN_STATICO, URL_TOKEN, URL_PEDIDOS, SLACK_SIGNING_SECRET]):
    required = [var for var, value in {
        "ARCO_API_KEY": TOKEN_STATICO,
        "ARCO_URL_TOKEN": URL_TOKEN,
        "ARCO_URL_PEDIDOS": URL_PEDIDOS,
        "SLACK_SIGNING_SECRET": SLACK_SIGNING_SECRET
    }.items() if not value]
    logger.critical(f"Variáveis de ambiente obrigatórias não configuradas: {', '.join(required)}. Encerrando.")
    sys.exit(1)

# --- Funções Auxiliares ---

def verify_slack_signature(request):
    """Verifica a assinatura da requisição do Slack."""
    if not SLACK_SIGNING_SECRET:
        return False

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

    return compare_digest(computed_sig, slack_signature)

def consultar_api_com_retry(url, payload, max_tentativas=5, intervalo_inicial=1, intervalo_maximo=60):
    """Consulta uma API com retry e timeout estendido (45s)."""
    tentativa = 0
    while tentativa < max_tentativas:
        tentativa += 1
        try:
            headers = {'Content-Type': 'application/json'}
            # TIMEOUT AUMENTADO PARA 45s PARA EVITAR ERROS DE COMUNICAÇÃO
            res = requests.post(url, json=payload, headers=headers, timeout=45)
            res.raise_for_status()
            return res.json()
        except (requests.exceptions.Timeout, requests.exceptions.RequestException) as e:
            logger.warning(f"Tentativa {tentativa}/{max_tentativas} falhou: {e}")
        
        if tentativa < max_tentativas:
            espera = min(intervalo_inicial * (2 ** (tentativa - 1)) + random.random(), intervalo_maximo)
            sleep(espera)
            
    raise Exception(f"Falha ao consultar a API externa após {max_tentativas} tentativas.")

def obter_chave_escola(pedido):
    """Gera a chave única da escola."""
    codigo_acesso = pedido.get('CodigoAcesso')
    if codigo_acesso:
        return str(codigo_acesso).strip()
    nome_exato = str(pedido.get('Escola') or '').strip()
    cep_escola = str(pedido.get('Cep') or '').strip()
    return f"{nome_exato}||{cep_escola}"

def formatar_lista_produtos(produtos_raw):
    """Formata a string de produtos em bullet points limpos."""
    if not produtos_raw:
        return "• Nenhum item informado"
    produtos_limpos = produtos_raw.replace('|', ',').split(',')
    return "\n".join([f"• {item.strip()}" for item in produtos_limpos if item.strip()])

# --- Lógica Principal ---

def process_slack_command(response_url, texto_comando_slack):
    logger.info(f"Processando: {texto_comando_slack}")

    def send_slack_message(response_url, text=None, blocks=None, response_type="in_channel"):
        payload = {"response_type": response_type}
        if blocks: payload["blocks"] = blocks
        elif text: payload["text"] = text
        try:
            requests.post(response_url, json=payload, timeout=10)
        except:
            pass

    try:
        partes = texto_comando_slack.strip().split()
        tipo_comando = partes[0].strip().lower()
        marca_api = partes[1].strip() if len(partes) > 1 else "nave"
        ano_projeto_api = int(partes[2]) if len(partes) > 2 and partes[2].isdigit() else 2025

        # 1. Token
        token_data = consultar_api_com_retry(URL_TOKEN, {"token": TOKEN_STATICO})
        token_autenticacao = token_data.get("retorno", {}).get("token")
        if not token_autenticacao:
            send_slack_message(response_url, text="Erro ao gerar token ARCO.")
            return

        # 2. Payload
        pedidos_payload = {
            "token": token_autenticacao, "Tipo": "pedido", "Marca": marca_api,
            "AnoProjeto": ano_projeto_api, "DataPedidoInicial": "", "DataPedidoFinal": "",
            "Despachavel": "S"
        }

        filtro_escola = None
        filtro_chave = None
        hoje = datetime.datetime.now()

        def get_year_range(ano):
            return f"{ano}-01-01 00:00:00", f"{ano}-12-31 23:59:59"

        if tipo_comando == "aging":
            dias = int(partes[3]) if len(partes) > 3 and partes[3].isdigit() else 7
            pedidos_payload["DataPedidoInicial"] = (hoje - datetime.timedelta(days=dias)).strftime("%Y-%m-%d 00:00:00")
            pedidos_payload["DataPedidoFinal"] = hoje.strftime("%Y-%m-%d 23:59:59")
        elif tipo_comando in ["pedido", "itens"]:
            pedidos_payload["Pedido"] = int(partes[3])
            pedidos_payload["DataPedidoInicial"], pedidos_payload["DataPedidoFinal"] = get_year_range(ano_projeto_api)
        elif tipo_comando == "escola" or tipo_comando == "escola_abertos":
            filtro_escola = " ".join(partes[3:]).strip().lower()
            pedidos_payload["DataPedidoInicial"], pedidos_payload["DataPedidoFinal"] = get_year_range(ano_projeto_api)
        elif tipo_comando in ["busca_chave", "busca_chave_abertos", "panorama"]:
            filtro_chave = " ".join(partes[3:]).strip()
            pedidos_payload["DataPedidoInicial"], pedidos_payload["DataPedidoFinal"] = get_year_range(ano_projeto_api)

        # 3. Consulta
        pedidos_brutos = consultar_api_com_retry(URL_PEDIDOS, pedidos_payload)
        if not isinstance(pedidos_brutos, list):
            send_slack_message(response_url, text="Erro na resposta da lista de pedidos.")
            return

        # 4. Filtros
        pedidos_filtrados = pedidos_brutos
        if filtro_escola:
            pedidos_filtrados = [p for p in pedidos_filtrados if filtro_escola in str(p.get("Escola") or "").lower()]
        if filtro_chave:
            pedidos_filtrados = [p for p in pedidos_filtrados if str(p.get("CodigoAcesso") or "").strip() == filtro_chave or f"{str(p.get('Escola') or '').strip()}||{str(p.get('Cep') or '').strip()}" == filtro_chave]
        
        if tipo_comando in ["escola_abertos", "busca_chave_abertos", "panorama"]:
            pedidos_filtrados = [p for p in pedidos_filtrados if "cancelado" not in str(p.get("StatusPedido") or "").lower() and "entrega realizada" not in str(p.get("StatusPedido") or "").lower() and "devoluç" not in str(p.get("StatusPedido") or "").lower()]

        pedidos_filtrados.sort(key=lambda p: int(p.get("idPedido") or 0) if str(p.get("idPedido") or 0).isdigit() else 0, reverse=True)

        if not pedidos_filtrados:
            send_slack_message(response_url, text="Nenhum pedido encontrado para os critérios.")
            return

        # Menu de Navegação Universal
        pedido_ref = pedidos_filtrados[0]
        chave_unica = obter_chave_escola(pedido_ref)
        val_nav = f"{marca_api}|{ano_projeto_api}|{chave_unica}"
        menu_nav = {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Ver 5 últimos"}, "value": val_nav, "action_id": "ver_pedidos_chave_unica"},
                {"type": "button", "text": {"type": "plain_text", "text": "Ver em aberto"}, "value": val_nav, "action_id": "ver_pedidos_abertos_chave_unica"},
                {"type": "button", "text": {"type": "plain_text", "text": "Panorama Geral"}, "value": val_nav, "action_id": "ver_panorama_escola"}
            ]
        }

        # 5. Formatação de Telas
        if tipo_comando == "itens":
            p = pedidos_filtrados[0]
            blocos = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"🔢 *Pedido:* {p.get('idPedido')}\n🏫 *Escola:* {p.get('Escola')}\n🚚 *Status:* {p.get('StatusPedido')}\n📅 *Data:* {p.get('DataPedido')}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*📦 Itens:*\n{formatar_lista_produtos(p.get('Produtos'))}"}},
                {"type": "divider"},
                menu_nav
            ]
            send_slack_message(response_url, blocks=blocos, response_type="ephemeral")
            return

        if tipo_comando == "panorama":
            blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": f"📊 *Panorama de Produtos em Andamento*\n🏫 *{pedido_ref.get('Escola')}*"}}, {"type": "divider"}]
            for p in pedidos_filtrados:
                id_p = p.get('idPedido')
                qtd_t = p.get('Qtd Produtos') or p.get('QtdProdutos') or '—'
                txt = f"🔢 *Pedido:* {id_p} | 📦 *Total:* {qtd_t} itens | 🚚 *Status:* {p.get('StatusPedido')}\n{formatar_lista_produtos(p.get('Produtos'))}"
                blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
                blocos.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": f"🔎 Detalhes do {id_p}"}, "value": f"{marca_api}|{ano_projeto_api}|{id_p}", "action_id": "ver_itens_pedido"}]})
                blocos.append({"type": "divider"})
            blocos.append(menu_nav)
            send_slack_message(response_url, blocks=blocos)
            return

        # Tela Padrão (5 Pedidos)
        blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": "*📦 Resultados encontrados:*"}}]
        for p in pedidos_filtrados[:5]:
            id_p = p.get('idPedido')
            qtd_t = p.get('Qtd Produtos') or p.get('QtdProdutos') or '—'
            status = p.get('StatusPedido') or '—'
            txt = f"🔢 *Pedido:* {id_p} | 📦 *Total:* {qtd_t} itens\n🏫 *Escola:* {p.get('Escola')}\n🚚 *Status:* {status}\n📅 *Data:* {p.get('DataPedido')}"
            
            # Adiciona Logística se disponível
            if 'despachado' in status.lower() or 'trânsito' in status.lower():
                txt += f"\n🚛 *Transporte:* {p.get('Transportadora')}\n🗓️ *Previsão:* {p.get('PrevisaoEntrega') or '—'}"

            blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
            blocos.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "📦 Ver Itens"}, "value": f"{marca_api}|{ano_projeto_api}|{id_p}", "action_id": "ver_itens_pedido"}]})
            blocos.append({"type": "divider"})
        blocos.append(menu_nav)
        send_slack_message(response_url, blocks=blocos)

    except Exception as e:
        logger.error(f"Erro: {e}", exc_info=True)
        send_slack_message(response_url, text="Ocorreu um erro inesperado.")

# --- Rotas Flask ---

@app.route("/", methods=["GET", "HEAD"])
def index(): return "OK", 200

@app.route("/keep-alive", methods=["GET"])
def keep_alive(): return "OK", 200

def start_keep_alive():
    if not RENDER_EXTERNAL_URL: return
    while True:
        sleep(600)
        try:
            agora_brt = datetime.datetime.utcnow() - datetime.timedelta(hours=3)
            if 4 <= agora_brt.hour < 22: requests.get(f"{RENDER_EXTERNAL_URL}/keep-alive", timeout=10)
        except: pass

@app.route("/slack/commands", methods=["POST"])
def slack_command():
    if not verify_slack_signature(request): return "Unauthorized", 401
    form = parse_qs(request.get_data().decode("utf-8"))
    text = form.get("text", [""])[0].strip()
    response_url = form.get("response_url", [""])[0].strip()
    threading.Thread(target=process_slack_command, args=(response_url, text)).start()
    return jsonify({"response_type": "ephemeral", "text": "🛠️ Processando consulta..."}), 200

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    if not verify_slack_signature(request): return "Unauthorized", 401
    payload = json.loads(request.form.get("payload"))
    response_url = payload.get("response_url")
    action = payload.get("actions", [{}])[0]
    aid, val = action.get("action_id"), action.get("value")

    cmds = {
        "ver_pedidos_chave_unica": "busca_chave",
        "ver_pedidos_abertos_chave_unica": "busca_chave_abertos",
        "ver_panorama_escola": "panorama",
        "ver_itens_pedido": "itens"
    }

    if aid in cmds and val:
        m, a, c = val.split("|", 2)
        threading.Thread(target=process_slack_command, args=(response_url, f"{cmds[aid]} {m} {a} {c}")).start()
    return "", 200

if __name__ == "__main__":
    threading.Thread(target=start_keep_alive, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
