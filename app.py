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
        logger.critical("verify_slack_signature chamada sem SLACK_SIGNING_SECRET configurado.")
        return False

    slack_signature = request.headers.get("X-Slack-Signature")
    slack_timestamp = request.headers.get("X-Slack-Request-Timestamp")
    if not slack_signature or not slack_timestamp:
        logger.error("Faltando X-Slack-Signature ou X-Slack-Request-Timestamp.")
        return False

    if abs(time() - float(slack_timestamp)) > 60 * 5:
        logger.error(f"Timestamp do Slack muito antigo: {slack_timestamp}")
        return False

    body = request.get_data().decode("utf-8")
    sig_basestring = f"v0:{slack_timestamp}:{body}".encode("utf-8")
    computed_sig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        sig_basestring,
        hashlib.sha256
    ).hexdigest()

    if not compare_digest(computed_sig, slack_signature):
        logger.warning("Assinatura do Slack inválida.")
        return False

    return True

def consultar_api_com_retry(url, payload, max_tentativas=5, intervalo_inicial=1, intervalo_maximo=60):
    """Consulta uma API com retry e backoff exponencial com jitter."""
    tentativa = 0
    while tentativa < max_tentativas:
        tentativa += 1
        try:
            headers = {'Content-Type': 'application/json'}
            res = requests.post(url, json=payload, headers=headers, timeout=15)
            res.raise_for_status()
            logger.info(f"Tentativa {tentativa}: Sucesso ao chamar {url}. Status: {res.status_code}")
            return res.json()
        except requests.exceptions.Timeout:
            logger.warning(f"Tentativa {tentativa}/{max_tentativas} falhou: Timeout ao chamar {url}")
        except requests.exceptions.HTTPError as e:
            logger.warning(f"Tentativa {tentativa}/{max_tentativas} falhou: Erro HTTP {e.response.status_code} ao chamar {url}: {e}")
            if e.response.status_code == 429:
                logger.warning("Recebemos um erro 429 (Too Many Requests).")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Tentativa {tentativa}/{max_tentativas} falhou: Erro de requisição ao chamar {url}: {e}")
        
        if tentativa < max_tentativas:
            espera = min(intervalo_inicial * (2 ** (tentativa - 1)) + random.random(), intervalo_maximo)
            logger.info(f"Tentativa {tentativa}: Falha. Próxima tentativa em {espera:.2f} segundos.")
            sleep(espera)
            
    logger.error(f"Falha ao consultar a API {url} após {max_tentativas} tentativas.")
    raise Exception(f"Falha ao consultar a API externa ({url}) após {max_tentativas} tentativas.")

# --- Lógica Principal (Executada em Thread) ---

def process_slack_command(response_url, texto_comando_slack):
    """
    Processa o comando Slack, interage com a API ARCO e envia a resposta de volta para o Slack.
    """
    logger.info(f"Iniciando processamento do comando em thread: {texto_comando_slack}")

    def send_slack_message(response_url, text=None, blocks=None, response_type="in_channel"):
        """Envia uma mensagem para o Slack, suportando texto simples ou blocos."""
        payload = {"response_type": response_type}
        if blocks:
            payload["blocks"] = blocks
        elif text:
            payload["text"] = text
        else:
            logger.error("send_slack_message chamada sem 'text' ou 'blocks'.")
            return
        try:
            requests.post(response_url, json=payload, timeout=10)
            log_msg = text if text else f"Blocks: {str(blocks)[:100]}..."
            logger.info(f"Mensagem enviada para Slack response_url: {log_msg}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Falha crítica ao enviar mensagem para response_url {response_url}: {e}")

    try:
        partes = texto_comando_slack.strip().split()
        if len(partes) < 2:
            send_slack_message(response_url, text="Formato incorreto. Use /comando <tipo> <argumentos>...")
            return

        tipo_comando = partes[0].strip().lower()
        marca_api = partes[1].strip() if len(partes) > 1 else "nave"
        ano_projeto_api_str = partes[2] if len(partes) > 2 and partes[2].isdigit() else "2025"
        ano_projeto_api = int(ano_projeto_api_str)

        # --- 1. Gerar Token de Autenticação ---
        logger.info("Gerando token de autenticação da API ARCO...")
        try:
            token_payload = {"token": TOKEN_STATICO}
            token_data = consultar_api_com_retry(URL_TOKEN, token_payload)
            retorno_data = token_data.get("retorno", {})
            status_integracao = retorno_data.get("statusIntegracao")
            token_autenticacao = retorno_data.get("token")
            msg_api = retorno_data.get("mensagens", {}).get("mensagem", "Resposta da API de token sem mensagem detalhada.")

            if status_integracao != "SUCESSO" or not token_autenticacao:
                logger.error(f"Falha na resposta da API ARCO /gerartoken. Status: {status_integracao}, Token Presente: {bool(token_autenticacao)}, Mensagem API: {msg_api}")
                send_slack_message(response_url, text=f"Erro ao gerar token da API ARCO. Detalhes: {msg_api}")
                return
            logger.info("Token ARCO gerado com sucesso.")
        except Exception as e:
             logger.error(f"Falha crítica ao gerar token da API ARCO (exceção): {e}", exc_info=True)
             send_slack_message(response_url, text=f"Erro ao comunicar com a API ARCO para gerar token: {e}")
             return

        # --- 2. Construir Payload para Consultar Pedidos ---
        pedidos_payload = {
            "token": token_autenticacao, "Tipo": "pedido", "Marca": marca_api,
            "AnoProjeto": ano_projeto_api, "DataPedidoInicial": "", "DataPedidoFinal": "",
            "Despachavel": "S"
        }

        filtro_escola = None
        filtro_chave = None
        hoje = datetime.datetime.now()

        def set_date_range_for_year(ano):
            try:
                inicio_ano = datetime.datetime(ano, 1, 1).strftime("%Y-%m-%d 00:00:00")
                fim_ano = datetime.datetime(ano, 12, 31, 23, 59, 59).strftime("%Y-%m-%d 23:59:59")
                return inicio_ano, fim_ano
            except ValueError:
                raise ValueError(f"Erro: Ano {ano} inválido para definir o intervalo de datas.")

        try:
            if tipo_comando == "aging":
                dias = int(partes[3]) if len(partes) > 3 and partes[3].isdigit() else 7
                inicio_data = hoje - datetime.timedelta(days=dias)
                pedidos_payload["DataPedidoInicial"] = inicio_data.strftime("%Y-%m-%d 00:00:00")
                pedidos_payload["DataPedidoFinal"] = hoje.strftime("%Y-%m-%d 23:59:59")

            elif tipo_comando == "pedido" or tipo_comando == "itens":
                if len(partes) < 4:
                    send_slack_message(response_url, text=f"Comando '{tipo_comando}' requer marca, ano e número do pedido.")
                    return
                filtro_numero_pedido_str = partes[3].strip()
                try:
                    pedidos_payload["Pedido"] = int(filtro_numero_pedido_str)
                except ValueError:
                    send_slack_message(response_url, text=f"Erro: O número do pedido '{filtro_numero_pedido_str}' não é um número válido.")
                    return
                pedidos_payload["DataPedidoInicial"], pedidos_payload["DataPedidoFinal"] = set_date_range_for_year(ano_projeto_api)

            elif tipo_comando == "expedicao":
                if len(partes) < 5:
                    send_slack_message(response_url, text="Comando 'expedicao' requer marca, ano, data inicial e final (AAAA-MM-DD).")
                    return
                data_inicial_str = partes[3].strip()
                data_final_str = partes[4].strip()
                datetime.datetime.strptime(data_inicial_str, "%Y-%m-%d") 
                datetime.datetime.strptime(data_final_str, "%Y-%m-%d") 
                pedidos_payload["DataPedidoInicial"] = f"{data_inicial_str} 00:00:00"
                pedidos_payload["DataPedidoFinal"] = f"{data_final_str} 23:59:59"

            elif tipo_comando == "escola" or tipo_comando == "escola_abertos":
                if len(partes) < 4:
                    send_slack_message(response_url, text="Comando 'escola' requer marca, ano e o nome (ou parte do nome) da escola.")
                    return
                filtro_escola = " ".join(partes[3:]).strip().lower()
                pedidos_payload["DataPedidoInicial"], pedidos_payload["DataPedidoFinal"] = set_date_range_for_year(ano_projeto_api)

            elif tipo_comando in ["busca_chave", "busca_chave_abertos", "panorama"]:
                if len(partes) < 4:
                    return
                filtro_chave = " ".join(partes[3:]).strip()
                pedidos_payload["DataPedidoInicial"], pedidos_payload["DataPedidoFinal"] = set_date_range_for_year(ano_projeto_api)

            else:
                send_slack_message(response_url, text=f"Tipo de consulta '{tipo_comando}' não reconhecido.")
                return
        except (ValueError, IndexError) as e:
            send_slack_message(response_url, text=f"Erro ao processar comando: {e}")
            return

        # --- 3. Consultar Pedidos na API ARCO ---
        logger.info(f"Consultando API ARCO de pedidos com payload (Despachavel=S)...")
        try:
            pedidos_brutos = consultar_api_com_retry(URL_PEDIDOS, pedidos_payload)
            if not isinstance(pedidos_brutos, list):
                msg_api_err = pedidos_brutos.get("retorno", {}).get("mensagens", {}).get("mensagem", "Resposta inesperada da API.")
                send_slack_message(response_url, text=f"Erro na resposta da API de pedidos: {msg_api_err}")
                return
        except Exception as e:
            logger.error(f"Falha crítica ao consultar API de Pedidos: {e}", exc_info=True)
            send_slack_message(response_url, text=f"Erro ao comunicar com a API ARCO para consultar pedidos: {e}")
            return

        # --- 4. Aplicar Filtros no Lado do Cliente ---
        pedidos_filtrados = pedidos_brutos
        
        # Filtros blindados com "or ''" para evitar TypeError/AttributeError caso a API mande null
        if filtro_escola:
            pedidos_filtrados = [
                p for p in pedidos_filtrados 
                if filtro_escola in str(p.get("Escola") or "").lower()
            ]

        if tipo_comando in ["busca_chave", "busca_chave_abertos", "panorama"] and filtro_chave:
            pedidos_filtrados = [
                p for p in pedidos_filtrados
                if str(p.get("CodigoAcesso") or "").strip() == filtro_chave or f"{str(p.get('Escola') or '').strip()}||{str(p.get('Cep') or '').strip()}" == filtro_chave
            ]

        if tipo_comando in ["escola_abertos", "busca_chave_abertos", "panorama"]:
            status_fechados = ['entrega realizada', 'cancelado', 'devolução finalizada']
            pedidos_filtrados = [
                p for p in pedidos_filtrados
                if str(p.get("StatusPedido") or "").lower() not in status_fechados
            ]

        # Ordenação blindada
        pedidos_filtrados.sort(
            key=lambda p: int(p.get("idPedido") or 0) if str(p.get("idPedido") or 0).isdigit() else 0, 
            reverse=True
        )

        # --- 5. Formatar e Enviar Resposta para o Slack ---
        if not pedidos_filtrados:
            msg_erro = "Nenhum pedido encontrado com os critérios especificados."
            if tipo_comando != "aging":
                 msg_erro += " (Lembrete: A busca considera apenas pedidos 'despacháveis' ou já enviados)."
            send_slack_message(response_url, text=msg_erro)
            return

        # =========================================================================
        # TELA EXCLUSIVA 1: DETALHE DOS ITENS (APENAS 1 PEDIDO)
        # =========================================================================
        if tipo_comando == "itens":
            pedido_unico = pedidos_filtrados[0]
            
            id_pedido_itens = pedido_unico.get('idPedido') or '—'
            escola = pedido_unico.get('Escola') or '—'
            cidade = pedido_unico.get('Cidade') or '—'
            uf = pedido_unico.get('Uf') or '—'
            status = pedido_unico.get('StatusPedido') or '—'
            data_pedido = pedido_unico.get('DataPedido') or '—'

            texto_detalhe_pedido = (
                f"🔢 *Número do pedido:* {id_pedido_itens}\n"
                f"🏫 *Escola:* {escola} - {cidade}/{uf}\n"
                f"🚚 *Status:* {status}\n"
                f"📅 *Data Pedido:* {data_pedido}"
            )

            produtos_raw = pedido_unico.get('Produtos') or 'Nenhum item encontrado.'
            produtos_formatados = "\n".join([item.strip() for item in produtos_raw.split(',') if item.strip()])
            
            blocos_de_resposta = [
                {"type": "section", "text": {"type": "mrkdwn", "text": texto_detalhe_pedido}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"📦 *Itens do Pedido:*"}},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{produtos_formatados}```"}}
            ]
            
            send_slack_message(response_url, blocks=blocos_de_resposta, response_type="ephemeral")
            return

        # =========================================================================
        # TELA EXCLUSIVA 2: PANORAMA GERAL DA ESCOLA (PRODUTOS EM ABERTO)
        # =========================================================================
        if tipo_comando == "panorama":
            pedido_base = pedidos_filtrados[0]
            
            # Gera a Chave Única para manter os botões de navegação no rodapé
            codigo_acesso = pedido_base.get('CodigoAcesso')
            if codigo_acesso:
                chave_para_botao = str(codigo_acesso).strip()
            else:
                nome_exato = str(pedido_base.get('Escola') or '').strip()
                cep_escola = str(pedido_base.get('Cep') or '').strip()
                chave_para_botao = f"{nome_exato}||{cep_escola}"
            
            escola_nome = pedido_base.get("Escola") or "Escola"
            
            blocos_de_resposta = [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"📊 *Panorama de Produtos em Andamento*\n🏫 *{escola_nome}*"}},
                {"type": "divider"}
            ]
            
            texto_panorama = ""
            for p in pedidos_filtrados:
                id_pedido = p.get('idPedido') or '—'
                status = p.get('StatusPedido') or '—'
                produtos_raw = p.get('Produtos') or 'Nenhum item informado'
                
                # Trata a lista de produtos
                produtos_lista = [item.strip() for item in produtos_raw.split(',') if item.strip()]
                produtos_formatados = "\n".join([f"• {item}" for item in produtos_lista])
                
                # Monta o bloquinho de texto para este pedido específico
                bloco_texto = f"🔢 *Pedido:* {id_pedido} | 🚚 *Status:* {status}\n{produtos_formatados}\n\n"
                
                # Evita estourar o limite de 3000 caracteres do Slack quebrando em vários blocos
                if len(texto_panorama) + len(bloco_texto) > 2800:
                    blocos_de_resposta.append({"type": "section", "text": {"type": "mrkdwn", "text": texto_panorama}})
                    texto_panorama = bloco_texto
                else:
                    texto_panorama += bloco_texto
                    
            if texto_panorama:
                blocos_de_resposta.append({"type": "section", "text": {"type": "mrkdwn", "text": texto_panorama}})
            
            # Adiciona os botões de navegação no rodapé
            valor_botao_escola = f"{marca_api}|{ano_projeto_api}|{chave_para_botao}"
            blocos_de_resposta.append({"type": "divider"})
            blocos_de_resposta.append({
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Ver 5 últimos", "emoji": True}, "value": valor_botao_escola, "action_id": "ver_pedidos_chave_unica"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Ver em aberto", "emoji": True}, "value": valor_botao_escola, "action_id": "ver_pedidos_abertos_chave_unica"},
                    {"type": "button", "text": {"type": "plain_text", "text": "🔄 Atualizar Panorama", "emoji": True}, "value": valor_botao_escola, "action_id": "ver_panorama_escola"}
                ]
            })
            
            send_slack_message(response_url, blocks=blocos_de_resposta)
            return

        # =========================================================================
        # TELA PADRÃO: LISTAGEM DOS 5 PEDIDOS
        # =========================================================================
        blocos_de_resposta = [{"type": "section", "text": {"type": "mrkdwn", "text": "*📦 Resultados encontrados:*"}}]
        chave_para_botao = None
        marca_para_botao = marca_api
        ano_para_botao = ano_projeto_api

        for i, p in enumerate(pedidos_filtrados[:5]):
            status_pedido = str(p.get('StatusPedido') or '—')
            status_lower = status_pedido.lower()
            
            if i == 0:
                codigo_acesso = p.get('CodigoAcesso')
                if codigo_acesso:
                    chave_para_botao = str(codigo_acesso).strip()
                else:
                    nome_exato = str(p.get('Escola') or '').strip()
                    cep_escola = str(p.get('Cep') or '').strip()
                    chave_para_botao = f"{nome_exato}||{cep_escola}"

            texto_do_pedido = ""
            texto_do_pedido += f"🔢 *Número do pedido:* {p.get('idPedido') or '—'}\n"
            texto_do_pedido += f"🏫 *Escola:* {p.get('Escola') or '—'} - {p.get('Cidade') or '—'}/{p.get('Uf') or '—'}\n"
            texto_do_pedido += f"🚚 *Status:* {status_pedido}\n"
            texto_do_pedido += f"📅 *Data Pedido:* {p.get('DataPedido') or '—'}\n"

            transportadora = p.get('Transportadora')
            data_expedicao = p.get('DataExpedicao')

            if 'despachado' in status_lower or 'em trânsito' in status_lower:
                previsao_entrega = p.get('PrevisaoEntrega')
                if transportadora:
                    texto_do_pedido += f"🚛 *Transportadora:* {transportadora}\n"
                if data_expedicao:
                    texto_do_pedido += f"📦 *Expedição:* {data_expedicao}\n"
                if previsao_entrega:
                    texto_do_pedido += f"🗓️ *Previsão Entrega:* {previsao_entrega}\n"

            elif 'entrega realizada' in status_lower:
                data_entrega_real = p.get('DataEntrega')
                if data_expedicao:
                    texto_do_pedido += f"📦 *Expedição:* {data_expedicao}\n"
                if data_entrega_real:
                    texto_do_pedido += f"✅ *Entrega Realizada:* {data_entrega_real}\n"
                if transportadora:
                    texto_do_pedido += f"🚛 *Transportadora:* {transportadora}\n"

            elif 'cancelado' in status_lower:
                motivo = p.get('MotivoCancelamento') or 'Não informado'
                texto_do_pedido += f"🚫 *Motivo Cancelamento:* {motivo}\n"

            blocos_de_resposta.append({"type": "section", "text": {"type": "mrkdwn", "text": texto_do_pedido}})

            # AGORA O BOTÃO "VER ITENS" APARECE PARA TODOS OS PEDIDOS
            id_pedido_item = p.get('idPedido')
            if id_pedido_item:
                valor_botao_item = f"{marca_api}|{ano_projeto_api}|{id_pedido_item}"
                blocos_de_resposta.append({
                    "type": "actions",
                    "elements": [{
                        "type": "button",
                        "text": {"type": "plain_text", "text": "📦 Ver Itens do Pedido", "emoji": True},
                        "value": valor_botao_item,
                        "action_id": "ver_itens_pedido"
                    }]
                })

            blocos_de_resposta.append({"type": "divider"})

        # INCLUI OS 3 BOTÕES NO RODAPÉ
        if chave_para_botao:
            valor_botao_escola = f"{marca_para_botao}|{ano_para_botao}|{chave_para_botao}"
            blocos_de_resposta.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Ver 5 últimos", "emoji": True},
                        "value": valor_botao_escola,
                        "action_id": "ver_pedidos_chave_unica"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Ver em aberto", "emoji": True},
                        "value": valor_botao_escola,
                        "action_id": "ver_pedidos_abertos_chave_unica"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Panorama de Produtos", "emoji": True},
                        "value": valor_botao_escola,
                        "action_id": "ver_panorama_escola"
                    }
                ]
            })

        if len(pedidos_filtrados) > 5:
            blocos_de_resposta.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"_Mostrando os primeiros 5 de {len(pedidos_filtrados)} pedidos encontrados._"}]
            })

        send_slack_message(response_url, blocks=blocos_de_resposta)

    except Exception as e:
        logger.error(f"Erro inesperado no processamento do comando Slack (thread): {str(e)}", exc_info=True)
        send_slack_message(response_url, text="Ocorreu um erro inesperado ao processar seu comando. Por favor, tente novamente.")


# --- Anti-Hibernação (Apenas horário comercial BRT) ---
@app.route("/keep-alive", methods=["GET"])
def keep_alive_route():
    """Rota simples apenas para o Render receber tráfego e não hibernar."""
    return "OK", 200

def start_keep_alive_loop():
    """Ping contínuo na própria API restrito das 04h às 22h (Horário de Brasília)."""
    if not RENDER_EXTERNAL_URL:
        logger.warning("RENDER_EXTERNAL_URL não encontrada. Anti-Hibernação desativado.")
        return

    while True:
        sleep(600)  # Pausa por 10 minutos (600 segundos)
        try:
            agora_utc = datetime.datetime.utcnow()
            agora_brt = agora_utc - datetime.timedelta(hours=3)
            
            if 4 <= agora_brt.hour < 22:
                logger.info(f"[{agora_brt.strftime('%H:%M:%S')} BRT] Executando ping de anti-hibernação...")
                requests.get(f"{RENDER_EXTERNAL_URL}/keep-alive", timeout=10)
            else:
                logger.info(f"[{agora_brt.strftime('%H:%M:%S')} BRT] Fora do horário comercial. API liberada para hibernar.")
                
        except Exception as e:
            logger.warning(f"Falha no ping de anti-hibernação: {e}")


# --- Rota Flask para Comandos Slack ---

@app.route("/slack/commands", methods=["POST"])
def slack_command():
    """Recebe comandos slash do Slack, verifica assinatura e inicia thread."""
    if not verify_slack_signature(request):
        return "Assinatura inválida ou requisição não verificada.", 401

    try:
        form = parse_qs(request.get_data().decode("utf-8"))
        text = form.get("text", [""])[0].strip()
        response_url = form.get("response_url", [""])[0].strip()
        if not response_url:
            return "Erro interno: response_url ausente.", 500
    except Exception as e:
        logger.error(f"Erro ao parsear requisição do Slack (rota): {e}", exc_info=True)
        return "Erro interno ao processar sua requisição.", 500

    logger.info(f"Comando Slack recebido: '{text}' para response_url: '{response_url}'")

    partes = text.split()
    if len(partes) < 3:
        return jsonify({
            "response_type": "ephemeral", 
            "text": "Formato incorreto. Use /comando <tipo> <marca> <ano> [argumentos]. Ex: /consulta pedido nave 2025 12345"
        }), 200

    try:
        thread = threading.Thread(target=process_slack_command, args=(response_url, text))
        thread.start()
    except Exception as e:
        logger.error(f"Falha ao iniciar thread de processamento: {e}", exc_info=True)
        try:
            requests.post(response_url, json={"text": "Erro interno: Não foi possível iniciar o processamento do comando."}, timeout=5)
        except requests.exceptions.RequestException:
            pass
        return "Erro interno do servidor ao iniciar processo.", 500

    return jsonify({"response_type": "ephemeral", "text": "🛠️ Sua consulta está sendo processada, aguarde..."}), 200


# --- Rota Flask para Interatividade (Botões) ---

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    """Recebe interações do Slack (cliques em botões)."""
    if not verify_slack_signature(request):
        return "Assinatura inválida.", 401

    try:
        payload_str = request.form.get("payload")
        if not payload_str:
            return "Payload ausente.", 400
        
        payload = json.loads(payload_str)
        response_url = payload.get("response_url")
        if not response_url:
            return "Erro interno.", 500

        if payload.get("type") == "block_actions":
            action = payload.get("actions", [{}])[0]
            action_id = action.get("action_id")
            action_value = action.get("value")

            novo_comando_texto = None
            mensagem_imediata = None
            
            try:
                if action_id == "ver_pedidos_chave_unica" and action_value:
                    marca, ano, chave_escola = action_value.split("|", 2)
                    novo_comando_texto = f"busca_chave {marca} {ano} {chave_escola}"
                    mensagem_imediata = f"Buscando os últimos 5 pedidos da escola..."

                elif action_id == "ver_pedidos_abertos_chave_unica" and action_value:
                    marca, ano, chave_escola = action_value.split("|", 2)
                    novo_comando_texto = f"busca_chave_abertos {marca} {ano} {chave_escola}"
                    mensagem_imediata = f"Buscando pedidos em aberto da escola..."
                    
                # ROTA PARA O NOVO BOTÃO PANORAMA
                elif action_id == "ver_panorama_escola" and action_value:
                    marca, ano, chave_escola = action_value.split("|", 2)
                    novo_comando_texto = f"panorama {marca} {ano} {chave_escola}"
                    mensagem_imediata = f"Gerando panorama de produtos em aberto..."

                elif action_id == "ver_itens_pedido" and action_value:
                    marca, ano, id_pedido = action_value.split("|", 2)
                    novo_comando_texto = f"itens {marca} {ano} {id_pedido}"
                    mensagem_imediata = f"Buscando itens do pedido *{id_pedido}*..."

                if novo_comando_texto and mensagem_imediata:
                    logger.info(f"Ação '{action_id}' recebida. Comando interno: {novo_comando_texto}")
                    
                    requests.post(response_url, json={
                        "response_type": "ephemeral", 
                        "text": mensagem_imediata
                    }, timeout=5)

                    thread = threading.Thread(target=process_slack_command, args=(response_url, novo_comando_texto))
                    thread.start()
                else:
                    logger.warning(f"Ação não reconhecida ou valor ausente: {action_id}")

            except Exception as e:
                logger.error(f"Erro ao processar ação '{action_id}': {e}")
                requests.post(response_url, json={"text": "Erro ao processar sua solicitação."}, timeout=5)

    except Exception as e:
        logger.error(f"Erro grave na rota /slack/interactive: {e}", exc_info=True)
        return "Erro interno.", 500

    return "", 200

# --- Execução ---

if __name__ == "__main__":
    logger.info("Configurações carregadas e verificadas.")
    
    keep_alive_thread = threading.Thread(target=start_keep_alive_loop, daemon=True)
    keep_alive_thread.start()
    logger.info("Sistema anti-hibernação (horário restrito) iniciado.")

    port = int(os.getenv("PORT", 5000))
    logger.info(f"Iniciando servidor Flask na porta {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
