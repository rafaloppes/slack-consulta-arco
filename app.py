from flask import Flask, request, jsonify
import requests
import datetime
import os
import threading
import logging
from urllib.parse import parse_qs
import hashlib
import hmac
from time import time
from hmac import compare_digest
import json

app = Flask(__name__)

# Configurações de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configurações da API ARCO e Slack
TOKEN_STATICO = os.getenv("ARCO_API_KEY", "R0VFS0lFLVJBSVpFUy0yMDI0")
URL_TOKEN = os.getenv("ARCO_URL_TOKEN", "https://webservice.raizessolucoes.com.br/arco/gerartoken")
URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS", "https://webservice.raizessolucoes.com.br/arco/pedidos")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")

# Verificação da assinatura do Slack
def verify_slack_signature(request):
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET não configurado, ignorando verificação")
        return True

    slack_signature = request.headers.get("X-Slack-Signature")
    slack_timestamp = request.headers.get("X-Slack-Request-Timestamp")
    if not slack_signature or not slack_timestamp:
        logger.error("Faltando X-Slack-Signature ou X-Slack-Request-Timestamp")
        return False

    if abs(time() - float(slack_timestamp)) > 60 * 5:
        logger.error("Timestamp do Slack muito antigo")
        return False

    body = request.get_data().decode("utf-8")
    sig_basestring = f"v0:{slack_timestamp}:{body}".encode("utf-8")
    computed_sig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        sig_basestring,
        hashlib.sha256
    ).hexdigest()

    return compare_digest(computed_sig, slack_signature)

# Lógica do comando Slack
def process_slack_command(response_url, texto):
    logger.info(f"Processando comando: {texto}")
    try:
        partes = texto.strip().split()
        if len(partes) < 2:
            requests.post(response_url, json={"text": "Formato incorreto. Ex: /consulta aging nave 2024 7 ou /consulta numero 579744"})
            return

        tipo = partes[0].strip()
        token_res = requests.post(URL_TOKEN, json={"token": TOKEN_STATICO}, timeout=5)
        logger.info(f"Resposta da API ARCO (Token): {token_res.text}")
        try:
            token_data = token_res.json()
            logger.info(f"Dados da Resposta da API ARCO (Token): {token_data}")
            logger.info(f"Tipo de token_data: {type(token_data)}")
            logger.info(f"Conteúdo de token_data: {token_data}")

            if "retorno" in token_data and \
               "statusintegracao" in token_data["retorno"] and \
               token_data["retorno"]["statusintegracao"] != "SUCESSO":
                requests.post(response_url, json={"text": f"Erro ao gerar token: {token_data['retorno']['mensagens']['mensagem']}"} )
                return

            if "retorno" in token_data and "token" in token_data["retorno"]:
                token = token_data["retorno"]["token"]
            else:
                logger.error("Estrutura de resposta da API ARCO (Token) inválida.")
                requests.post(response_url, json={"text": "Erro ao processar a resposta da API ARCO."})
                return

        except json.JSONDecodeError as e:
            logger.error(f"Erro ao decodificar JSON: {e}")
            requests.post(response_url, json={"text": "Erro ao processar a resposta da API ARCO."})
            return
        except KeyError as e:
            logger.error(f"Erro ao acessar campo na resposta: {e}")
            requests.post(response_url, json={"text": "Erro ao processar a resposta da API ARCO."})
            return

        payload = {
            "token": token,
            "Tipo": "pedido",
            "Marca": partes[1].strip() if len(partes) > 1 else "nave",
            "AnoProjeto": int(partes[2]) if len(partes) > 2 else 2025,  # Alterado para 2025 (ou para datetime.datetime.now().year para o ano atual)
        }

        if tipo == "aging":
            dias = int(partes[3]) if len(partes) > 3 else 7
            hoje = datetime.datetime.now()
            inicio = hoje - datetime.timedelta(days=dias)
            payload["DataPedidoInicial"] = inicio.strftime("%Y-%m-%d 00:00:00")
            payload["DataPedidoFinal"] = hoje.strftime("%Y-%m-%d 23:59:59")
        elif tipo == "numero" or (tipo == "consulta" and len(partes) == 2 and partes[1].isdigit()):
            payload["numero_pedido"] = partes[1].strip() if len(partes) > 1 else partes[1]
        elif tipo == "expedicao":
            payload["DataPedidoInicial"] = f"{partes[1].strip()} 00:00:00" if len(partes) > 1 else ""
            payload["DataPedidoFinal"] = f"{partes[2].strip()} 23:59:59" if len(partes) > 2 else ""
        elif tipo == "escola":
            escola = partes[1].lower().strip() if len(partes) > 1 else ""

        try:
            res = requests.post(URL_PEDIDOS, json=payload, timeout=10)
            logger.info(f"Código de Status da API ARCO (Pedidos): {res.status_code}")
            logger.info(f"Resposta da API ARCO (Pedidos): {res.text}")
            if res.status_code == 200:  # Verifique se o código de status é 200
                pedidos = res.json()
                logger.info(f"Dados da Resposta da API ARCO (Pedidos): {pedidos}")
            else:
                logger.error(f"Erro na API ARCO: Código de Status {res.status_code}")
                requests.post(response_url, json={"text": f"Erro na API ARCO: Código de Status {res.status_code}"})
                return
        except requests.exceptions.RequestException as e:
            logger.error(f"Erro ao consultar a API de Pedidos: {e}")
            requests.post(response_url, json={"text": "Erro ao consultar a API ARCO."})
            return
        except json.JSONDecodeError as e:
            logger.error(f"Erro ao decodificar JSON (Pedidos): {e}")
            requests.post(response_url, json={"text": "Erro ao processar a resposta da API ARCO."})
            return

        if tipo == "numero" or (tipo == "consulta" and len(partes) == 2 and partes[1].isdigit()):
            pedidos = [p for p in pedidos if str(p.get("PedidoOrigem")) == payload["numero_pedido"]]
        elif tipo == "escola":
            pedidos = [p for p in pedidos if escola in p["Escola"].lower()]

        if not pedidos:
            requests.post(response_url, json={"text": "Nenhum pedido encontrado."})
            return

        resposta = "*📦 Resultados encontrados:*\n"
        for p in pedidos[:5]:
            resposta += (
                f"\n🏫 *Escola:* {p['Escola']} - {p['Cidade']}/{p['Uf']}\n"
                f"📦 *Produtos:* {p['Produtos']} ({p['QtdProdutos']} itens)\n"
                f"💲 *Valor:* R$ {p['ValorFinalPedido']:.2f}\n"
                f"🚚 *Status:* {p['StatusPedido']}\n"
                f"📅 *Data Pedido:* {p['DataPedido']}\n"
                f"📦 *Expedição:* {p.get('DataExpedicao') or 'Ainda não expedido'}\n"
                f"📧 {p.get('Email') or '—'} | 📞 {p.get('Telefone') or '—'}\n"
                "— — — — — — — —\n"
            )

        requests.post(response_url, json={"response_type": "in_channel", "text": resposta})
    except Exception as e:
        logger.error(f"Erro no processamento: {str(e)}")
        requests.post(response_url, json={"text": f"Erro: {str(e)}"})

# Endpoint que recebe a chamada do Slack
@app.route("/slack/consulta", methods=["POST"])
def consulta():
    logger.info("Recebida requisição para /slack/consulta")
    
    if not verify_slack_signature(request):
        logger.error("Assinatura do Slack inválida")
        return jsonify({"text": "Assinatura do Slack inválida."}), 403

    try:
        form_data = parse_qs(request.get_data().decode("utf-8"))
        text = form_data.get("text", [""])[0]
        response_url = form_data.get("response_url", [""])[0]
    except Exception as e:
        logger.error(f"Erro ao parsear form data: {str(e)}")
        return jsonify({"text": "Erro ao processar a requisição."}), 400

    # Iniciar processamento em segundo plano
    threading.Thread(target=process_slack_command, args=(response_url, text)).start()

    # Resposta imediata para evitar timeout no Slack
    logger.info("Enviando resposta imediata ao Slack")
    return jsonify({"text": "Processando sua consulta..."}), 200

# Rodar servidor
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
