from flask import Flask, request, jsonify
import requests
import datetime
import os
import threading
import logging
from urllib.parse import parse_qs
import hashlib
import hmac
from hmac import compare_digest # Importado corretamente
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
# Removendo os valores padrão hardcoded. Estas variáveis DEVEM ser configuradas.
TOKEN_STATICO = os.getenv("ARCO_API_KEY")
URL_TOKEN = os.getenv("ARCO_URL_TOKEN")
URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# --- Verificação na Inicialização ---
# Verificar se as variáveis de ambiente obrigatórias estão configuradas
# Esta verificação é importante para garantir que a aplicação não inicie com configurações incompletas ou inseguras
if not all([TOKEN_STATICO, URL_TOKEN, URL_PEDIDOS, SLACK_SIGNING_SECRET]):
    required = [var for var, value in {
        "ARCO_API_KEY": TOKEN_STATICO,
        "ARCO_URL_TOKEN": URL_TOKEN,
        "ARCO_URL_PEDIDOS": URL_PEDIDOS,
        "SLACK_SIGNING_SECRET": SLACK_SIGNING_SECRET
    }.items() if not value]
    logger.critical(f"Variáveis de ambiente obrigatórias não configuradas: {', '.join(required)}. Encerrando.")
    sys.exit(1) # Encerra o aplicativo se as configurações críticas estiverem faltando

# --- Funções Auxiliares ---

# Verificação da assinatura do Slack
def verify_slack_signature(request):
    # Já verificamos SLACK_SIGNING_SECRET na inicialização, então ele deve existir aqui.
    # Se, por algum motivo, chegar aqui sem ele, é um erro grave.
    if not SLACK_SIGNING_SECRET:
        logger.critical("verify_slack_signature chamada sem SLACK_SIGNING_SECRET configurado. Configuração inválida.")
        return False # NUNCA retorne True se o segredo não estiver configurado

    slack_signature = request.headers.get("X-Slack-Signature")
    slack_timestamp = request.headers.get("X-Slack-Request-Timestamp")
    if not slack_signature or not slack_timestamp:
        logger.error("Faltando X-Slack-Signature ou X-Slack-Request-Timestamp na requisição do Slack.")
        return False

    # Verificar se o timestamp é muito antigo (mais de 5 minutos)
    if abs(time() - float(slack_timestamp)) > 60 * 5:
        logger.error(f"Timestamp do Slack muito antigo: {slack_timestamp}")
        return False

    # Reconstruir a base string e calcular a assinatura
    body = request.get_data().decode("utf-8")
    sig_basestring = f"v0:{slack_timestamp}:{body}".encode("utf-8")
    computed_sig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        sig_basestring,
        hashlib.sha256
    ).hexdigest()

    # Comparar assinaturas de forma segura
    if not compare_digest(computed_sig, slack_signature):
        logger.warning("Assinatura do Slack inválida.")
        return False

    return True # Assinatura válida

# Função para consultar a API com retry e backoff
def consultar_api_com_retry(url, payload, max_tentativas=5, intervalo_inicial=1, intervalo_maximo=60):
    """
    Consulta uma API com retry e backoff exponencial com jitter.
    Retorna a resposta JSON em caso de sucesso.
    Levanta exceção em caso de falha após todas as tentativas.
    """
    tentativa = 0
    while tentativa < max_tentativas:
        tentativa += 1
        try:
            headers = {'Content-Type': 'application/json'}
            res = requests.post(url, json=payload, headers=headers, timeout=15)
            res.raise_for_status()  # Levanta exceção para códigos de status ruins (4xx ou 5xx)

            logger.info(f"Tentativa {tentativa}: Sucesso ao chamar {url}. Status: {res.status_code}")
            # Tenta logar o corpo da resposta para debug, especialmente para o token
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
            # Tenta logar corpo da resposta de erro também
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
    Esta função deve ser executada em um thread separado.
    """
    logger.info(f"Iniciando processamento do comando em thread: {texto_comando_slack}")
    # Usar response_url para enviar todas as respostas (sucesso, erro, etc.)
    def send_slack_message(message, response_type="in_channel"):
        try:
            # Garante que o response_url é o fornecido pelo Slack
            # Usa response_type="in_channel" como padrão para a maioria das respostas
            requests.post(response_url, json={"response_type": response_type, "text": message}, timeout=10)
            logger.info(f"Mensagem enviada para Slack response_url: {message[:100]}...") # Loga o início da mensagem
        except requests.exceptions.RequestException as e:
            logger.error(f"Falha crítica ao enviar mensagem para response_url {response_url}: {e}")

    try: # <--- Este é o try principal da função
        partes = texto_comando_slack.strip().split()
        # A validação básica de contagem já ocorreu na rota, mas pode ser reforçada
        if len(partes) < 2:
            send_slack_message("Formato incorreto. Use /comando <tipo> <argumentos>. Tipos: aging, numero, expedicao, escola.")
            return

        tipo_comando = partes[0].strip().lower() # Converte para minúsculas
        marca_api = partes[1].strip() if len(partes) > 1 else "nave" # Default 'nave' se não especificado
        # AnoProjeto padrão 2025, pode ser sobrescrito
        ano_projeto_api_str = partes[2] if len(partes) > 2 and partes[2].isdigit() else "2025"
        ano_projeto_api = int(ano_projeto_api_str)


        # --- 1. Gerar Token de Autenticação ---
        logger.info("Gerando token de autenticação da API ARCO...")
        try:
            # payload para gerar token conforme documentação
            token_payload = {"token": TOKEN_STATICO}
            token_data = consultar_api_com_retry(URL_TOKEN, token_payload)

            # --- Nova Verificação de Sucesso do Token ---
            # Verifica se o statusintegracao é SUCESSO E se o campo 'token' existe e não é vazio
            # Adicionado tratamento para garantir que token_data e token_data.get("retorno") não sejam None
            retorno_data = token_data.get("retorno", {})
            status_integracao = retorno_data.get("statusintegracao")
            token_autenticacao = retorno_data.get("token")
            # Tenta obter a mensagem da API, com fallback
            msg_api = retorno_data.get("mensagens", {}).get("mensagem", "Resposta da API de token sem mensagem detalhada.")

            # Condição de erro: status NÃO é SUCESSO, OU (||) se o token_autenticacao é um valor "falso" (None, string vazia, etc.)
            if status_integracao != "SUCESSO" or not token_autenticacao:
                logger.error(f"Falha na resposta da API ARCO /gerartoken. Status: {status_integracao}, Token Presente: {bool(token_autenticacao)}, Mensagem API: {msg_api}")
                # Enviar mensagem de erro para o Slack.
                # Tenta dar um feedback mais específico dependendo da falha
                if status_integracao == "SUCESSO" and not token_autenticacao:
                     send_slack_message("Erro interno: A API de token retornou sucesso, mas não forneceu um token válido.")
                elif status_integracao is None:
                     send_slack_message(f"Erro na resposta da API de token: Estrutura de resposta inesperada ou 'statusintegracao' ausente. Mensagem da API: {msg_api}")
                else: # status_integracao é ERRO ou outro valor não SUCESSO
                     send_slack_message(f"Erro ao gerar token da API ARCO. Status API: {status_integracao}. Detalhes: {msg_api}")
                return # Interrompe o processamento se o token não foi gerado corretamente

            # Se passou pela condição acima, o token foi gerado com sucesso e está em token_autenticacao
            logger.info("Token ARCO gerado com sucesso.")
            # token_autenticacao já contém o token válido

            # --- 2. Construir Payload para Consultar Pedidos ---
            # Payload base com campos obrigatórios (exceto datas, que variam)
            pedidos_payload = {
                "token": token_autenticacao, # Usar o token recém-gerado
                "Tipo": "pedido",          # Fixo conforme documentação
                "Marca": marca_api,
                "AnoProjeto": ano_projeto_api,
                "DataPedidoInicial": "",   # Será preenchido abaixo
                "DataPedidoFinal": "",     # Será preenchido abaixo
            }

            filtro_numero_pedido = None
            filtro_escola = None

            # Definir intervalo de datas e filtros baseados no tipo de comando
            hoje = datetime.datetime.now()

            if tipo_comando == "aging":
                # /consulta aging [marca] [ano] [dias]
                dias = int(partes[3]) if len(partes) > 3 and partes[3].isdigit() else 7
                inicio_data = hoje - datetime.timedelta(days=dias)
                pedidos_payload["DataPedidoInicial"] = inicio_data.strftime("%Y-%m-%d 00:00:00")
                pedidos_payload["DataPedidoFinal"] = hoje.strftime("%Y-%m-%d 23:59:59")

            elif tipo_comando == "numero":
                # /consulta numero [marca] [ano] [numero_pedido]
                if len(partes) < 4:
                     send_slack_message("Comando 'numero' requer marca, ano e número do pedido. Ex: /consulta numero nave 2025 12345")
                     return
                filtro_numero_pedido = partes[3].strip() # Captura o número do pedido

                # A API não filtra por numero_pedido na requisição.
                # Precisamos definir um intervalo de datas amplo o suficiente para pegar o pedido.
                # Usa o ano do projeto especificado ou um período recente.
                # Buscar nos últimos 2 anos para cobrir a maioria dos casos razoáveis
                data_inicio_busca = hoje - datetime.timedelta(days=365*2) # Últimos 2 anos
                # Opcional: Ajustar para o início do ano do projeto se for mais recente
                # inicio_ano_proj = datetime.datetime(ano_projeto_api, 1, 1)
                # data_inicio_busca = min(data_inicio_busca, inicio_ano_proj) # Buscar a partir do início do ano do projeto ou últimos 2 anos, o que for mais recente

                pedidos_payload["DataPedidoInicial"] = data_inicio_busca.strftime("%Y-%m-%d 00:00:00")
                pedidos_payload["DataPedidoFinal"] = hoje.strftime("%Y-%m-%d 23:59:59")
                logger.info(f"Buscando pedidos entre {pedidos_payload['DataPedidoInicial']} e {pedidos_payload['DataPedidoFinal']} para filtrar por número {filtro_numero_pedido}")

            elif tipo_comando == "expedicao":
                # /consulta expedicao [marca] [ano] [data_inicial-AAAA-MM-DD] [data_final-AAAA-MM-DD]
                if len(partes) < 5:
                    send_slack_message("Comando 'expedicao' requer marca, ano, data inicial e final (AAAA-MM-DD). Ex: /consulta expedicao nave 2025 2025-01-01 2025-01-31")
                    return
                try:
                    # === Validação e Formatação das Datas de Expedição ===
                    data_inicial_str = partes[3].strip()
                    data_final_str = partes[4].strip()
                    # Tenta parsear as datas no formato esperado AAAA-MM-DD
                    datetime.datetime.strptime(data_inicial_str, "%Y-%m-%d")
                    datetime.datetime.strptime(data_final_str, "%Y-%m-%d") # Corrigido para %Y-%m-%d

                    # Se parseou com sucesso, formata para o padrão da API AAAA-MM-DD HH:mm:ss
                    pedidos_payload["DataPedidoInicial"] = f"{data_inicial_str} 00:00:00"
                    pedidos_payload["DataPedidoFinal"] = f"{data_final_str} 23:59:59"

                except ValueError:
                    send_slack_message("Formato de data incorreto para 'expedicao'. Use AAAA-MM-DD.")
                    return

            elif tipo_comando == "escola":
                # /consulta escola [marca] [ano] [nome da escola]
                if len(partes) < 4:
                     send_slack_message("Comando 'escola' requer marca, ano e o nome (ou parte do nome) da escola. Ex: /consulta escola geekie 2024 'Nome da Escola'")
                     return
                # Pega o restante das partes como o nome da escola (suporta nomes com espaços)
                filtro_escola = " ".join(partes[3:]).strip().lower()

                # A API não filtra por nome da escola na requisição.
                # Assim como no filtro por número, precisamos definir um intervalo de datas amplo.
                data_inicio_busca = hoje - datetime.timedelta(days=365*2) # Últimos 2 anos
                # Opcional: Ajustar para o início do ano do projeto se for mais recente
                # inicio_ano_proj = datetime.datetime(ano_projeto_api, 1, 1)
                # data_inicio_busca = min(data_inicio_busca, inicio_ano_proj)

                pedidos_payload["DataPedidoInicial"] = data_inicio_busca.strftime("%Y-%m-%d 00:00:00")
                pedidos_payload["DataPedidoFinal"] = hoje.strftime("%Y-%m-%d 23:59:59")
                logger.info(f"Buscando pedidos entre {pedidos_payload['DataPedidoInicial']} e {pedidos_payload['DataPedidoFinal']} para filtrar por escola '{filtro_escola}'")

            else:
                # Tipo de comando não reconhecido (já validado parcialmente na rota, mas reforça)
                 send_slack_message(f"Tipo de consulta '{tipo_comando}' não reconhecido. Use 'aging', 'numero', 'expedicao' ou 'escola'.")
                 return

            # --- 3. Consultar Pedidos na API ARCO ---
            logger.info(f"Consultando API ARCO de pedidos com payload para datas entre {pedidos_payload['DataPedidoInicial']} e {pedidos_payload['DataPedidoFinal']}...")
            # logger.debug(f"Payload completo: {pedidos_payload}") # Use debug para não logar o token em INFO

            try: # <--- Este é outro try block dentro do try principal
                pedidos_brutos = consultar_api_com_retry(URL_PEDIDOS, pedidos_payload)

                if not isinstance(pedidos_brutos, list):
                     logger.error(f"Resposta inesperada da API /pedidos. Esperava lista, recebeu: {pedidos_brutos}")
                     # Tenta extrair mensagem de erro da API se houver, com fallbacks
                     msg_api_err = pedidos_brutos.get("retorno", {}).get("mensagens", {}).get("mensagem", "Resposta inesperada ou vazia da API de pedidos.")
                     send_slack_message(f"Erro na resposta da API de pedidos: {msg_api_err}")
                     return

                logger.info(f"Recebidos {len(pedidos_brutos)} pedidos brutos da API.")

            except Exception as e: # <--- Este é o except para a consulta de pedidos
                logger.error(f"Falha crítica ao consultar API de Pedidos: {e}", exc_info=True)
                send_slack_message(f"Erro ao comunicar com a API ARCO para consultar pedidos: {e}")
                return

            # --- 4. Aplicar Filtros no Lado do Cliente (se aplicável) ---
            pedidos_filtrados = pedidos_brutos

            if filtro_numero_pedido:
                 pedidos_filtrados = [
                     p for p in pedidos_filtrados
                     # Usar .get para evitar KeyError, converter para string para comparação
                     if str(p.get("PedidoOrigem", "")) == filtro_numero_pedido
                 ]
                 logger.info(f"Após filtrar por número {filtro_numero_pedido}: {len(pedidos_filtrados)} pedidos encontrados.")

            elif filtro_escola:
                 pedidos_filtrados = [
                     p for p in pedidos_filtrados
                     # Usar .get('Escola', '') para evitar erro se o campo estiver ausente
                     if filtro_escola in p.get("Escola", "").lower()
                 ]
                 logger.info(f"Após filtrar por escola '{filtro_escola}': {len(pedidos_filtrados)} pedidos encontrados.")


            # --- 5. Formatar e Enviar Resposta para o Slack ---
            if not pedidos_filtrados:
                send_slack_message("Nenhum pedido encontrado com os critérios especificados.")
                return

            resposta = "*📦 Resultados encontrados:*\n"
            # Limita a 5 resultados para não exceder o limite de mensagem do Slack facilmente,
            # mas informa se houver mais.
            for i, p in enumerate(pedidos_filtrados[:5]):
                 # Usar .get() com valor padrão para evitar KeyError se um campo estiver ausente
                 resposta += (
                    f"\n🏫 *Escola:* {p.get('Escola', '—')} - {p.get('Cidade', '—')}/{p.get('Uf', '—')}\n"
                    f"📦 *Produtos:* {p.get('Produtos', '—')} ({p.get('Qtd Produtos', '—')} itens)\n"
                    f"💲 *Valor:* R$ {p.get('ValorFinalPedido', 0.0):.2f}\n" # Default 0.0 para formatar float
                    f"🚚 *Status:* {p.get('StatusPedido', '—')}\n"
                    f"📅 *Data Pedido:* {p.get('DataPedido', '—')}\n"
                    # A documentação mostra DataExpedicao como campo.
                    f"📦 *Expedição:* {p.get('DataExpedicao') or 'Ainda não expedido'}\n"
                    f"📧 {p.get('Email') or '—'} | 📞 {p.get('Telefone') or '—'}\n"
                    f"ID Origem: {p.get('PedidoOrigem', '—')}\n" # Adiciona PedidoOrigem conforme visto na doc
                    "— — — — — — — —\n"
                )

            # Adicionar mensagem se houver mais de 5 resultados
            if len(pedidos_filtrados) > 5:
                resposta += f"\n_Mostrando os primeiros 5 de {len(pedidos_filtrados)} pedidos encontrados._"

            send_slack_message(resposta)
            logger.info("Resposta final enviada para o Slack.")

    except Exception as e: # <--- Este é o 'except Exception' final da função 'process_slack_command'
        # Este bloco captura qualquer erro *inesperado* que não foi tratado antes
        logger.error(f"Erro inesperado no processamento do comando Slack (thread): {str(e)}", exc_info=True)
        # Evita enviar detalhes técnicos completos no erro geral para o usuário
        send_slack_message("Ocorreu um erro inesperado ao processar seu comando. Por favor, tente novamente.")

# --- Rota Flask para Comandos Slack --- # <--- Esta linha deve estar no nível raiz, SEM INDENTAÇÃO

@app.route("/slack/commands", methods=["POST"])
def slack_command():
    """
    Recebe comandos slash do Slack, verifica a assinatura e inicia
    o processamento do comando em um thread separado para evitar timeout.
    """
    # 1. Verificar Assinatura do Slack
    if not verify_slack_signature(request):
        # verify_slack_signature já loga o motivo
        return "Assinatura inválida ou requisição não verificada.", 401 # Retorna 401 Unauthorized

    # 2. Parsear a Requisição do Slack (form-urlencoded)
    try:
        form = parse_qs(request.get_data().decode("utf-8"))
        # Acessar os valores da lista retornada por parse_qs
        text = form.get("text", [""])[0].strip()
        response_url = form.get("response_url", [""])[0].strip()
        # token = form.get("token", [""])[0] # Opcional: verificar o token do Slack, mas a assinatura é mais segura
        # user_id = form.get("user_id", [""])[0] # Opcional: saber quem executou o comando
        # channel_id = form("channel_id", [""])[0] # Opcional: saber onde foi executado

        if not response_url:
             logger.error("response_url ausente na requisição do Slack.")
             # Não há como responder ao usuário sem isso, loga e retorna erro 500 para o Slack
             return "Erro interno: response_url ausente.", 500

    except Exception as e:
         logger.error(f"Erro ao parsear requisição do Slack (rota): {e}", exc_info=True)
         # Retorna erro 500 se não conseguir nem parsear a requisição
         return "Erro interno ao processar sua requisição.", 500

    logger.info(f"Comando Slack recebido: '{text}' para response_url: '{response_url}'")

    # 3. Validar Formato Básico do Comando Imediatamente
    partes = text.split()
    # O formato mínimo esperado pelo process_slack_command AGORA é tipo + marca + ano
    if len(partes) < 3:
         # Não temos a response_url no caso de parse_qs falhar totalmente, mas se o parse
         # deu certo, temos a response_url para enviar a mensagem de erro formatada.
         # Tentar enviar mensagem via response_url, mas estar preparado para falha.
         try:
             send_slack_message("Formato incorreto. Use /comando <tipo> <marca> <ano> [argumentos]. Ex: /consulta numero nave 2025 12345")
         except Exception as e:
             logger.error(f"Falha ao enviar mensagem de formato incorreto para response_url: {e}")
         # Retorna 200 OK mesmo assim para o Slack, pois a falha foi na validação inicial do formato.
         # Uma mensagem imediata no Slack pode ser útil aqui.
         return jsonify({"response_type": "in_channel", "text": "Erro de formato. Verifique a ajuda do comando."}), 200


    # 4. Iniciar Processamento Completo em um Thread Separado
    # Isso libera rapidamente o worker do Flask para responder ao Slack.
    try:
        # Passa a response_url e o texto completo do comando para a função de processamento.
        thread = threading.Thread(target=process_slack_command, args=(response_url, text))
        thread.start()
        logger.info("Thread de processamento iniciada.")

    except Exception as e:
        logger.error(f"Falha ao iniciar thread de processamento: {e}", exc_info=True)
         # Se a thread não puder ser iniciada, tentamos avisar o usuário via response_url
        try:
            requests.post(response_url, json={"text": "Erro interno: Não foi possível iniciar o processamento do comando."}, timeout=5)
        except requests.exceptions.RequestException:
             logger.error("Falha ao enviar mensagem de erro para response_url após falha da thread.")
        # Mesmo em caso de falha ao iniciar thread, a rota principal deve tentar retornar algo para o Slack
        return "Erro interno do servidor ao iniciar processo.", 500 # Retorna erro para o Slack também

    # 5. Retornar Resposta Imediata de Sucesso (200 OK) para o Slack
    # Esta é a resposta que o Slack espera em até 3 segundos.
    # O texto aqui será exibido imediatamente para o usuário.
    return jsonify({"response_type": "in_channel", "text": "🛠️ Sua consulta está sendo processada, aguarde..."}), 200


# --- Execução ---

if __name__ == "__main__":
    # A verificação das variáveis de ambiente já acontece no topo do script
    logger.info("Configurações carregadas e verificadas.")

    port = int(os.getenv("PORT", 5000))
    logger.info(f"Iniciando servidor Flask na porta {port}")
    # debug=False é recomendado para produção
    # use_reloader=False é recomendado quando se usa threading para evitar a duplicação de threads
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
