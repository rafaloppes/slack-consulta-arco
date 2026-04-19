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

def consultar_api_com_retry(url, payload, max_tentativas=3, intervalo_inicial=1, intervalo_maximo=15):
    tentativa = 0
    while tentativa < max_tentativas:
        tentativa += 1
        try:
            headers = {'Content-Type': 'application/json'}
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
    codigo_acesso = pedido.get('CodigoAcesso')
    if codigo_acesso: return str(codigo_acesso).strip()
    nome_exato = str(pedido.get('Escola') or '').strip()
    cep_escola = str(pedido.get('Cep') or '').strip()
    return f"{nome_exato}||{cep_escola}"

def formatar_lista_produtos(produtos_raw):
    if not produtos_raw: return "• Nenhum item informado"
    produtos_limpos = produtos_raw.replace('|', ',').split(',')
    return "\n".join([f"• {item.strip()}" for item in produtos_limpos if item.strip()])

def obter_qtd_total(p):
    return p.get('Qtd Produtos') or p.get('QtdProdutos') or '—'

# --- Lógica Principal ---

def process_slack_command(response_url, texto_comando_slack):
    logger.info(f"Processando: {texto_comando_slack}")

    def send_slack_message(response_url, text=None, blocks=None, response_type="in_channel"):
        payload = {
            "response_type": response_type,
            "replace_original": True
        }
        if blocks: payload["blocks"] = blocks
        elif text: payload["text"] = text
        try: requests.post(response_url, json=payload, timeout=10)
        except: pass

    try:
        partes = texto_comando_slack.strip().split()
        tipo_comando = partes[0].strip().lower()
        marca_api = partes[1].strip() if len(partes) > 1 else "nave"
        ano_projeto_api = int(partes[2]) if len(partes) > 2 and partes[2].isdigit() else 2025
        
        offset = int(partes[-1]) if partes[-1].isdigit() and len(partes) > 4 else 0

        # 1. Autenticação
        token_data = consultar_api_com_retry(URL_TOKEN, {"token": TOKEN_STATICO})
        token_autenticacao = token_data.get("retorno", {}).get("token")
        if not token_autenticacao:
            send_slack_message(response_url, text="Erro ao gerar token ARCO.")
            return

        # 2. Preparação da Busca
        pedidos_payload = {
            "token": token_autenticacao, "Tipo": "pedido", "Marca": marca_api,
            "AnoProjeto": ano_projeto_api, "DataPedidoInicial": "", "DataPedidoFinal": "",
            "Despachavel": "S"
        }

        filtro_escola = None
        filtro_chave = None
        hoje = datetime.datetime.now()
        def get_year_range(ano): return f"{ano}-01-01 00:00:00", f"{ano}-12-31 23:59:59"

        if tipo_comando == "aging":
            dias = int(partes[3]) if len(partes) > 3 and partes[3].isdigit() else 7
            pedidos_payload["DataPedidoInicial"] = (hoje - datetime.timedelta(days=dias)).strftime("%Y-%m-%d 00:00:00")
            pedidos_payload["DataPedidoFinal"] = hoje.strftime("%Y-%m-%d 23:59:59")
        elif tipo_comando in ["pedido", "itens"]:
            pedidos_payload["Pedido"] = int(partes[3])
            pedidos_payload["DataPedidoInicial"], pedidos_payload["DataPedidoFinal"] = get_year_range(ano_projeto_api)
        elif tipo_comando in ["escola", "escola_abertos"]:
            filtro_escola = " ".join(partes[3:] if not partes[-1].isdigit() else partes[3:-1]).strip().lower()
            pedidos_payload["DataPedidoInicial"], pedidos_payload["DataPedidoFinal"] = get_year_range(ano_projeto_api)
        elif tipo_comando in ["busca_chave", "busca_chave_abertos", "busca_chave_finalizados", "panorama"]:
            filtro_chave = " ".join(partes[3:] if not partes[-1].isdigit() else partes[3:-1]).strip()
            pedidos_payload["DataPedidoInicial"], pedidos_payload["DataPedidoFinal"] = get_year_range(ano_projeto_api)

        # 3. Consulta à API
        pedidos_brutos = consultar_api_com_retry(URL_PEDIDOS, pedidos_payload)
        if not isinstance(pedidos_brutos, list):
            send_slack_message(response_url, text="Erro na resposta da API ou dados não encontrados.")
            return

        # 4. Filtros de Negócio
        pedidos_filtrados = pedidos_brutos
        if filtro_escola:
            pedidos_filtrados = [p for p in pedidos_filtrados if filtro_escola in str(p.get("Escola") or "").lower()]
        if filtro_chave:
            pedidos_filtrados = [p for p in pedidos_filtrados if str(p.get("CodigoAcesso") or "").strip() == filtro_chave or f"{str(p.get('Escola') or '').strip()}||{str(p.get('Cep') or '').strip()}" == filtro_chave]
        
        # Filtros de Status estritos
        if tipo_comando in ["escola_abertos", "busca_chave_abertos", "panorama"]:
            # Exclui entrega realizada e cancelado rigorosamente, além de devoluções
            pedidos_filtrados = [p for p in pedidos_filtrados if all(x not in str(p.get("StatusPedido") or "").lower() for x in ["cancelado", "entrega realizada", "devoluç"])]
        
        elif tipo_comando == "busca_chave_finalizados":
            # Inclui estritamente entrega realizada OU cancelado, e barra devoluções
            pedidos_filtrados = [
                p for p in pedidos_filtrados 
                if any(x in str(p.get("StatusPedido") or "").lower() for x in ["entrega realizada", "cancelado"]) 
                and "devoluç" not in str(p.get("StatusPedido") or "").lower()
            ]

        pedidos_filtrados.sort(key=lambda p: int(p.get("idPedido") or 0) if str(p.get("idPedido") or 0).isdigit() else 0, reverse=True)

        if not pedidos_filtrados:
            send_slack_message(response_url, text="Nenhum pedido encontrado para os critérios selecionados.")
            return

        pedido_ref = pedidos_filtrados[0]
        chave_unica = obter_chave_escola(pedido_ref)
        val_nav = f"{marca_api}:::{ano_projeto_api}:::{chave_unica}"
        
        menu_botoes = [
            {"type": "button", "text": {"type": "plain_text", "text": "🏁 Finalizados / Cancelados"}, "value": val_nav, "action_id": "ver_pedidos_finalizados_chave_unica"},
            {"type": "button", "text": {"type": "plain_text", "text": "⏳ Ver em aberto"}, "value": val_nav, "action_id": "ver_pedidos_abertos_chave_unica"},
            {"type": "button", "text": {"type": "plain_text", "text": "📊 Panorama de Produtos"}, "value": val_nav, "action_id": "ver_panorama_escola"}
        ]

        # 5. Construção das Telas
        if tipo_comando == "itens":
            p = pedidos_filtrados[0]
            txt_topo = f"🔢 *Número do pedido:* {p.get('idPedido')}\n🏫 *Escola:* {p.get('Escola')}\n🚚 *Status:* {p.get('StatusPedido')}\n📅 *Data Pedido:* {p.get('DataPedido')}"
            blocos = [
                {"type": "section", "text": {"type": "mrkdwn", "text": txt_topo}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"📦 *Itens do Pedido (Total: {obter_qtd_total(p)} itens):*\n{formatar_lista_produtos(p.get('Produtos'))}"}},
                {"type": "divider"},
                {"type": "actions", "elements": menu_botoes}
            ]
            send_slack_message(response_url, blocks=blocos, response_type="ephemeral")
            return

        if tipo_comando == "panorama":
            blocos = [{"type": "section", "text": {"type": "mrkdwn", "text": f"📊 *Panorama de Produtos em Andamento*\n🏫 *{pedido_ref.get('Escola')}*"}}, {"type": "divider"}]
            for p in pedidos_filtrados:
                id_p = p.get('idPedido')
                txt = f"🔢 *Pedido:* {id_p} | 📦 *Total:* {obter_qtd_total(p)} itens | 🚚 *Status:* {p.get('StatusPedido')}\n{formatar_lista_produtos(p.get('Produtos'))}"
                blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
                blocos.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": f"🔎 Detalhes do {id_p}"}, "value": f"{marca_api}:::{ano_projeto_api}:::{id_p}", "action_id": "ver_itens_pedido"}]})
                blocos.append({"type": "divider"})
            blocos.append({"type": "actions", "elements": menu_botoes})
            send_slack_message(response_url, blocks=blocos)
            return

        resumo_status = ""
        if tipo_comando in ["escola_abertos", "busca_chave_abertos"]:
            counts = {}
            for p in pedidos_filtrados:
                s = p.get("StatusPedido") or "Desconhecido"
                counts[s] = counts.get(s, 0) + 1
            resumo_status = f"📊 *Resumo de Pedidos em Aberto (Total: {len(pedidos_filtrados)})*\n🏫 *{pedido_ref.get('Escola')}*\n"
            for s, c in counts.items(): resumo_status += f"• *{c}* - `{s}`\n"
            resumo_status += "---"

        elif tipo_comando == "busca_chave_finalizados":
            resumo_status = f"🏁 *Histórico de Pedidos Finalizados/Cancelados (Total: {len(pedidos_filtrados)})*\n🏫 *{pedido_ref.get('Escola')}*\n---"

        blocos = []
        if resumo_status: blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": resumo_status}})
        
        pedidos_pagina = pedidos_filtrados[offset : offset + 5]
        
        for p in pedidos_pagina:
            id_p = p.get('idPedido')
            status = p.get('StatusPedido') or '—'
            status_lower = status.lower()
            
            if tipo_comando in ["escola_abertos", "busca_chave_abertos"]:
                expedicao = p.get('DataExpedicao')
                txt = f"🔢 *{id_p}* | 📦 {obter_qtd_total(p)} itens | 🚚 {status}"
                if expedicao: txt += f"\n📤 *Expedido em:* {expedicao}"
                
            elif tipo_comando == "busca_chave_finalizados":
                txt = f"🔢 *{id_p}* | 📦 {obter_qtd_total(p)} itens | 🚚 {status}"
                if 'entrega realizada' in status_lower:
                    dt_entrega = p.get('DataEntrega')
                    if dt_entrega: txt += f"\n✅ *Entregue em:* {dt_entrega}"
                elif 'cancelado' in status_lower:
                    motivo = p.get('MotivoCancelamento') or 'Não informado'
                    txt += f"\n🚫 *Motivo:* {motivo}"
                    
            else:
                txt = f"🔢 *Número do pedido:* {id_p} | 📦 *Total:* {obter_qtd_total(p)} itens\n🏫 *Escola:* {p.get('Escola')}\n🚚 *Status:* {status}\n📅 *Data Pedido:* {p.get('DataPedido')}"
                if 'despachado' in status_lower or 'trânsito' in status_lower:
                    txt += f"\n🚛 *Transportadora:* {p.get('Transportadora') or '—'}"
                elif 'entrega realizada' in status_lower:
                    dt_entrega = p.get('DataEntrega')
                    if dt_entrega: txt += f"\n✅ *Entrega Realizada:* {dt_entrega}"
                elif 'cancelado' in status_lower:
                    motivo = p.get('MotivoCancelamento') or 'Não informado'
                    txt += f"\n🚫 *Motivo Cancelamento:* {motivo}"

            blocos.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
            blocos.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": f"🔎 Ver Detalhes do {id_p}"}, "value": f"{marca_api}:::{ano_projeto_api}:::{id_p}", "action_id": "ver_itens_pedido"}]})
            blocos.append({"type": "divider"})

        if len(pedidos_filtrados) > (offset + 5):
            prox_offset = offset + 5
            total_restante = len(pedidos_filtrados) - prox_offset
            mostrar = 5 if total_restante > 5 else total_restante
            
            action_id_paginacao = "ver_pedidos_chave_unica" 
            if "abertos" in tipo_comando: action_id_paginacao = "ver_pedidos_abertos_chave_unica"
            elif "finalizados" in tipo_comando: action_id_paginacao = "ver_pedidos_finalizados_chave_unica"
            
            btn_paginacao = {
                "type": "button", 
                "text": {"type": "plain_text", "text": f"➕ Ver próximos {mostrar} (Total: {len(pedidos_filtrados)})"}, 
                "value": f"{marca_api}:::{ano_projeto_api}:::{chave_unica}:::{prox_offset}", 
                "action_id": action_id_paginacao
            }
            menu_botoes.append(btn_paginacao)

        blocos.append({"type": "actions", "elements": menu_botoes})
        
        fim_idx = offset + 5 if len(pedidos_filtrados) > offset + 5 else len(pedidos_filtrados)
        blocos.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_Mostrando {offset+1} a {fim_idx} de {len(pedidos_filtrados)} pedidos._"}]})
        
        send_slack_message(response_url, blocks=blocos)

    except Exception as e:
        logger.error(f"Erro: {e}", exc_info=True)
        send_slack_message(response_url, text="Ocorreu um erro inesperado ao processar os dados.")

# --- Rotas Flask ---

@app.route("/", methods=["GET", "HEAD"])
def index(): return "Bot Ativo", 200

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
    return jsonify({"response_type": "ephemeral", "text": "🛠️ Processando consulta, aguarde..."}), 200

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
        "ver_pedidos_finalizados_chave_unica": "busca_chave_finalizados",
        "ver_panorama_escola": "panorama",
        "ver_itens_pedido": "itens"
    }

    if aid in cmds and val:
        partes = val.split(":::")
        m, a, c = partes[0], partes[1], partes[2]
        offs = partes[3] if len(partes) > 3 else "0"
        
        threading.Thread(target=process_slack_command, args=(response_url, f"{cmds[aid]} {m} {a} {c} {offs}")).start()
    
    return "", 200

if __name__ == "__main__":
    threading.Thread(target=start_keep_alive, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
