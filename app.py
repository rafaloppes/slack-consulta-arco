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
# Configurações de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configurações da API ARCO e Slack
TOKEN_STATICO = os.getenv("ARCO_API_KEY")
URL_TOKEN = os.getenv("ARCO_URL_TOKEN")
URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

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
        logger.critical("verify_slack_signature chamada sem SLACK_SIGNING_SECRET configurado. Configuração inválida.")
        return False

    slack_signature = request.headers.get("X-Slack-Signature")
    slack_timestamp = request.headers.get("X-Slack-Request-Timestamp")
    if not slack_signature or not slack_timestamp:
        logger.error("Faltando X-Slack-Signature ou X-Slack-Request-Timestamp na requisição do Slack.")
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
    """
    Consulta uma API com retry e backoff exponencial com jitter.
    """
    tentativa = 0
    while tentativa < max_tentativas:
        tentativa += 1
        try:
            headers = {'Content-Type': 'application/json'}
            res = requests.post(url, json=payload, headers=headers, timeout=15)
            res.raise_for_status()

            logger.info(f"Tentativa {tentativa}: Sucesso ao chamar {url}. Status: {res.status_code}")
            try:
                logger.info(f"Resposta API ({url}): {res.text}")
            except Exception as log_e:
                logger.warning(f"Não foi possível logar o corpo da resposta: {log_e}")

            return res.json()

        except requests.exceptions.Timeout:
            logger.warning(f"Tentativa {tentativa}/{max_tentativas} falhou: Timeout ao chamar {url}")
        except requests.exceptions.HTTPError as e:
            logger.warning(f"Tentativa {tentativa}/{max_tentativas} falhou: Erro HTTP {e.response.status_code} ao chamar {url}: {e}")
            if e.response.status_code == 429:
                logger.warning("Recebemos um erro 429 (Too Many Requests).")
            try:
                logger.warning(f"Corpo da resposta de erro ({url}): {e.response.text}")
            except Exception as log_e:
                logger.warning(f"Não foi possível logar o corpo da resposta de erro: {log_e}")
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
    
    # ATENÇÃO: Esta função de envio será modificada no PASSO 3
    def send_slack_message(message, response_type="in_channel"):
        try:
            requests.post(response_url, json={"response_type": response_type, "text": message}, timeout=10)
            logger.info(f"Mensagem enviada para Slack response_url: {message[:100]}...")
        except requests.exceptions.RequestException as e:
            logger.error(f"Falha crítica ao enviar mensagem para response_url {response_url}: {e}")

    try:
        partes = texto_comando_slack.strip().split()
        if len(partes) < 2:
            send_slack_message("Formato incorreto. Use /comando <tipo> <argumentos>...")
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
                if status_integracao == "SUCESSO" and not token_autenticacao:
                    send_slack_message("Erro interno: A API de token retornou sucesso, mas não forneceu um token válido.")
                elif status_integracao is None:
                    send_slack_message(f"Erro na resposta da API de token: Estrutura de resposta inesperada ou 'statusIntegracao' ausente. Mensagem da API: {msg_api}")
                else:
                    send_slack_message(f"Erro ao gerar token da API ARCO. Status API: {status_integracao}. Detalhes: {msg_api}")
                return
            logger.info("Token ARCO gerado com sucesso.")
        except Exception as e:
             logger.error(f"Falha crítica ao gerar token da API ARCO (exceção): {e}", exc_info=True)
             send_slack_message(f"Erro ao comunicar com a API ARCO para gerar token (exceção): {e}")
             return

        # --- 2. Construir Payload para Consultar Pedidos ---
        pedidos_payload = {
            "token": token_autenticacao,
            "Tipo": "pedido",
            "Marca": marca_api,
            "AnoProjeto": ano_projeto_api,
            "DataPedidoInicial": "",
            "DataPedidoFinal": "",
            "Despachavel": "S" # Filtro fixo conforme sua solicitação
        }

        filtro_escola = None
        hoje = datetime.datetime.now()

        if tipo_comando == "aging":
            dias = int(partes[3]) if len(partes) > 3 and partes[3].isdigit() else 7
            inicio_data = hoje - datetime.timedelta(days=dias)
            pedidos_payload["DataPedidoInicial"] = inicio_data.strftime("%Y-%m-%d 00:00:00")
            pedidos_payload["DataPedidoFinal"] = hoje.strftime("%Y-%m-%d 23:59:59")

        elif tipo_comando == "pedido":
            if len(partes) < 4:
                send_slack_message("Comando 'pedido' requer marca, ano e número do pedido. Ex: /consulta pedido nave 2025 12345")
                return
            filtro_numero_pedido_str = partes[3].strip()
            try:
                pedidos_payload["Pedido"] = int(filtro_numero_pedido_str)
            except ValueError:
                send_slack_message(f"Erro: O número do pedido '{filtro_numero_pedido_str}' não é um número válido.")
                return
            try:
                inicio_ano_proj = datetime.datetime(ano_projeto_api, 1, 1)
                fim_ano_proj = datetime.datetime(ano_projeto_api, 12, 31, 23, 59, 59)
                pedidos_payload["DataPedidoInicial"] = inicio_ano_proj.strftime("%Y-%m-%d 00:00:00")
                pedidos_payload["DataPedidoFinal"] = fim_ano_proj.strftime("%Y-%m-%d 23:59:59")
            except ValueError:
                send_slack_message(f"Erro: Ano {ano_projeto_api} inválido para definir o intervalo de datas.")
                return
            logger.info(f"Buscando pedido {filtro_numero_pedido_str} no ano {ano_projeto_api} via API.")

        elif tipo_comando == "expedicao":
            if len(partes) < 5:
                send_slack_message("Comando 'expedicao' requer marca, ano, data inicial e final (AAAA-MM-DD).")
                return
            try:
                data_inicial_str = partes[3].strip()
                data_final_str = partes[4].strip()
                datetime.datetime.strptime(data_inicial_str, "%Y-%m-%d")
                datetime.datetime.strptime(data_final_str, "%Y-%m-%d")
                pedidos_payload["DataPedidoInicial"] = f"{data_inicial_str} 00:00:00"
                pedidos_payload["DataPedidoFinal"] = f"{data_final_str} 23:59:59"
            except ValueError:
                send_slack_message("Formato de data incorreto para 'expedicao'. Use AAAA-MM-DD.")
                return

        elif tipo_comando == "escola":
            if len(partes) < 4:
                send_slack_message("Comando 'escola' requer marca, ano e o nome (ou parte do nome) da escola.")
                return
            filtro_escola = " ".join(partes[3:]).strip().lower()
            try:
                inicio_ano_proj = datetime.datetime(ano_projeto_api, 1, 1)
                fim_ano_proj = datetime.datetime(ano_projeto_api, 12, 31, 23, 59, 59)
                pedidos_payload["DataPedidoInicial"] = inicio_ano_proj.strftime("%Y-%m-%d 00:00:00")
                pedidos_payload["DataPedidoFinal"] = fim_ano_proj.strftime("%Y-%m-%d 23:59:59")
            except ValueError:
                send_slack_message(f"Erro: Ano {ano_projeto_api} inválido para definir o intervalo de datas.")
                return
            logger.info(f"Buscando pedidos para o ano de {ano_projeto_api} para filtrar por escola '{filtro_escola}'")

        else:
            send_slack_message(f"Tipo de consulta '{tipo_comando}' não reconhecido. Use 'aging', 'pedido', 'expedicao' ou 'escola'.")
            return

        # --- 3. Consultar Pedidos na API ARCO ---
        logger.info(f"Consultando API ARCO de pedidos com payload (Despachavel=S)...")
        try:
            pedidos_brutos = consultar_api_com_retry(URL_PEDIDOS, pedidos_payload)
            if not isinstance(pedidos_brutos, list):
                logger.error(f"Resposta inesperada da API /pedidos. Esperava lista, recebeu: {pedidos_brutos}")
                msg_api_err = pedidos_brutos.get("retorno", {}).get("mensagens", {}).get("mensagem", "Resposta inesperada ou vazia da API de pedidos.")
                send_slack_message(f"Erro na resposta da API de pedidos: {msg_api_err}")
                return
            logger.info(f"Recebidos {len(pedidos_brutos)} pedidos brutos da API.")
        except Exception as e:
            logger.error(f"Falha crítica ao consultar API de Pedidos: {e}", exc_info=True)
            send_slack_message(f"Erro ao comunicar com a API ARCO para consultar pedidos: {e}")
            return

        # --- 4. Aplicar Filtros no Lado do Cliente ---
        pedidos_filtrados = pedidos_brutos
        
        if filtro_escola:
            pedidos_filtrados = [
                p for p in pedidos_filtrados
                if filtro_escola in p.get("Escola", "").lower()
            ]
            logger.info(f"Após filtrar por escola '{filtro_escola}': {len(pedidos_filtrados)} pedidos encontrados.")

        # --- 5. Formatar e Enviar Resposta para o Slack ---
        if not pedidos_filtrados:
            msg_erro = "Nenhum pedido encontrado com os critérios especificados."
            if tipo_comando == "pedido" or tipo_comando == "escola":
                 msg_erro += " (Lembrete: A busca considera apenas pedidos 'despacháveis' ou já enviados)."
            send_slack_message(msg_erro)
            return

        resposta = "*📦 Resultados encontrados:*\n"
        
        for i, p in enumerate(pedidos_filtrados[:5]):
            status_pedido = p.get('StatusPedido', '—')
            status_lower = status_pedido.lower()

            resposta += (
                f"\n🔢 *Número do pedido:* {p.get('idPedido', '—')}\n"
                f"🏫 *Escola:* {p.get('Escola', '—')} - {p.get('Cidade', '—')}/{p.get('Uf', '—')}\n"
                f"🚚 *Status:* {status_pedido}\n"
                f"📅 *Data Pedido:* {p.get('DataPedido', '—')}\n"
            )

            # --- AJUSTE DE REGRA (DataExpedicao) ---
            # Pega os valores que podem ser usados em múltiplos status
            transportadora = p.get('Transportadora')
            data_expedicao = p.get('DataExpedicao')

            # 1. Lógica para "Em trânsito" ou "Despachado"
            if 'despachado' in status_lower or 'em trânsito' in status_lower:
                previsao_entrega = p.get('PrevisaoEntrega')
                if transportadora:
                    resposta += f"🚛 *Transportadora:* {transportadora}\n"
                if data_expedicao:
                    resposta += f"📦 *Expedição:* {data_expedicao}\n"
                if previsao_entrega:
                    resposta += f"🗓️ *Previsão Entrega:* {previsao_entrega}\n"

            # 2. Lógica para "Entrega realizada"
            elif 'entrega realizada' in status_lower:
                data_entrega_real = p.get('DataEntrega')
                if data_entrega_real:
                    resposta += f"✅ *Entrega Realizada:* {data_entrega_real}\n"
                if transportadora:
                    resposta += f"🚛 *Transportadora:* {transportadora}\n"
                # Exibe data de expedição também para pedidos entregues
                if data_expedicao:
                    resposta += f"📦 *Expedição:* {data_expedicao}\n"

            # 3. Lógica para "Cancelado"
            elif 'cancelado' in status_lower:
                motivo = p.get('MotivoCancelamento') or 'Não informado'
                resposta += f"🚫 *Motivo Cancelamento:* {motivo}\n"
            # --- Fim da Lógica ---

            resposta += "— — — — — — — —\n"

        if len(pedidos_filtrados) > 5:
            resposta += f"\n_Mostrando os primeiros 5 de {len(pedidos_filtrados)} pedidos encontrados._"

        send_slack_message(resposta)
        logger.info("Resposta final enviada para o Slack.")

    except Exception as e:
        logger.error(f"Erro inesperado no processamento do comando Slack (thread): {str(e)}", exc_info=True)
        send_slack_message("Ocorreu um erro inesperado ao processar seu comando. Por favor, tente novamente.")

# --- Rota Flask para Comandos Slack ---

@app.route("/slack/commands", methods=["POST"])
def slack_command():
    """
    Recebe comandos slash do Slack, verifica a assinatura e inicia
    o processamento do comando em um thread separado para evitar timeout.
    """
    if not verify_slack_signature(request):
        return "Assinatura inválida ou requisição não verificada.", 401

    try:
        form = parse_qs(request.get_data().decode("utf-8"))
        text = form.get("text", [""])[0].strip()
        response_url = form.get("response_url", [""])[0].strip()

        if not response_url:
            logger.error("response_url ausente na requisição do Slack.")
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
        logger.info("Thread de processamento iniciada.")
    except Exception as e:
        logger.error(f"Falha ao iniciar thread de processamento: {e}", exc_info=True)
        try:
            requests.post(response_url, json={"text": "Erro interno: Não foi possível iniciar o processamento do comando."}, timeout=5)
        except requests.exceptions.RequestException:
            logger.error("Falha ao enviar mensagem de erro para response_url após falha da thread.")
        return "Erro interno do servidor ao iniciar processo.", 500

    return jsonify({"response_type": "ephemeral", "text": "🛠️ Sua consulta está sendo processada, aguarde..."}), 200


# --- PASSO 2: NOVA ROTA INTERATIVA ---
# Este é o novo código que adicionamos
@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    """
    Recebe interações do Slack (cliques em botões).
    """
    # 1. Verificar a Assinatura (reutiliza a mesma função)
    if not verify_slack_signature(request):
        logger.warning("Assinatura inválida na rota interativa.")
        return "Assinatura inválida.", 401

    # 2. Parsear o 'payload'
    try:
        payload_str = request.form.get("payload")
        if not payload_str:
            logger.error("Payload interativo ausente.")
            return "Payload ausente.", 400
        
        payload = json.loads(payload_str)
        logger.info(f"Payload interativo recebido: {payload.get('type')}")

        response_url = payload.get("response_url")
        if not response_url:
            logger.error("response_url ausente no payload interativo.")
            return "Erro interno.", 500

        # Verifica se é uma ação de um bloco (clique em botão)
        if payload.get("type") == "block_actions":
            action = payload.get("actions", [{}])[0]
            action_id = action.get("action_id")
            action_value = action.get("value")

            # 3. Processar a Ação Específica
            if action_id == "ver_pedidos_escola" and action_value:
                logger.info(f"Ação 'ver_pedidos_escola' recebida com valor: {action_value}")
                
                # O valor é "marca|ano|nome_escola"
                try:
                    marca, ano, nome_escola = action_value.split("|", 2)
                    
                    novo_comando_texto = f"escola {marca} {ano} {nome_escola}"
                    
                    # Envia uma resposta imediata para o usuário (visível só para ele)
                    requests.post(response_url, json={
                        "response_type": "ephemeral", 
                        "text": f"Buscando os últimos 5 pedidos para a escola: *{nome_escola}*..."
                    }, timeout=5)

                    # 4. Inicia o processamento em um novo thread
                    thread = threading.Thread(target=process_slack_command, args=(response_url, novo_comando_texto))
                    thread.start()

                except Exception as e:
                    logger.error(f"Erro ao processar ação 'ver_pedidos_escola': {e}")
                    requests.post(response_url, json={"text": "Erro ao processar sua solicitação."}, timeout=5)

    except Exception as e:
        logger.error(f"Erro grave na rota /slack/interactive: {e}", exc_info=True)
        return "Erro interno.", 500

    # 5. Resposta imediata para o Slack
    return "", 200
# --- FIM DO NOVO BLOCO ---


# --- Execução ---

if __name__ == "__main__":
    logger.info("Configurações carregadas e verificadas.")
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Iniciando servidor Flask na porta {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
