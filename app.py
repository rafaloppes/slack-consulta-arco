from flask import Flask, request, jsonify
import requests
import datetime
import os
from hmac import HMAC, compare_digest
import hashlib

app = Flask(__name__)

# Configurações da API ARCO e Slack
TOKEN_STATICO = os.getenv("ARCO_API_KEY", "R0VFS0lFLVJBSVpFUy0yMDI0")
URL_TOKEN = os.getenv("ARCO_URL_TOKEN", "https://webservice.raizessolucoes.com.br/arco/gerartoken")
URL_PEDIDOS = os.getenv("ARCO_URL_PEDIDOS", "https://webservice.raizessolucoes.com.br/arco/pedidos")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# Verificar assinatura do Slack para segurança
def verify_slack_signature(request):
    if not SLACK_SIGNING_SECRET:
        return True  # Ignora verificação se não configurado (não recomendado para produção)
    slack_signature = request.headers.get("X-Slack-Signature")
    slack_timestamp = request.headers.get("X-Slack-Request-Timestamp")
    if not slack_signature or not slack_timestamp:
        return False
    body = request.get_data().decode("utf-8")
    sig_basestring = f"v0:{slack_timestamp}:{body}".encode("utf-8")
    computed_sig = "v0=" + HMAC(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        sig_basestring,
        hashlib.sha256
    ).hexdigest()
    return compare_digest(computed_sig, slack_signature)

@app.route("/slack/consulta", methods=["POST"])
def consulta():
    # Verificar assinatura do Slack
    if not verify_slack_signature(request):
        return jsonify({"text": "Assinatura do Slack inválida."}), 403

    try:
        texto = request.form.get("text", "")
        partes = texto.split()

        if len(partes) < 2:
            return jsonify({"text": "Formato incorreto. Ex: /consulta aging nave 2024 7"}), 200

        tipo = partes[0]
        token_res = requests.post(URL_TOKEN, json={"token": TOKEN_STATICO})
        token_data = token_res.json()
        if token_data["retorno"]["statusintegracao"] != "SUCESSO":
            return jsonify({"text": f"Erro ao gerar token: {token_data['retorno']['mensagens']['mensagem']}"}), 200
        token = token_data["retorno"]["token"]

        payload = {
            "token": token,
            "Tipo": "pedido",
            "Marca": partes[1] if len(partes) > 1 else "nave",
            "AnoProjeto": int(partes[2]) if len(partes) > 2 else 2024,
            "DataPedidoInicial": "2024-01-01 00:00:00",
            "DataPedidoFinal": "2024-12-31 23:59:59"
        }

        if tipo == "aging":
            dias = int(partes[3]) if len(partes) > 3 else 7
            hoje = datetime.datetime.now()
            inicio = hoje - datetime.timedelta(days=dias)
            payload["DataPedidoInicial"] = inicio.strftime("%Y-%m-%d 00:00:00")
            payload["DataPedidoFinal"] = hoje.strftime("%Y-%m-%d 23:59:59")

        elif tipo == "numero":
            numero = partes[1]
            payload["Marca"] = "nave"
            payload["AnoProjeto"] = 2024

        elif tipo == "expedicao":
            payload["DataPedidoInicial"] = f"{partes[2]} 00:00:00"
            payload["DataPedidoFinal"] = f"{partes[3]} 23:59:59"

        elif tipo == "escola":
            escola = partes[1].lower()

        res = requests.post(URL_PEDIDOS, json=payload)
        pedidos = res.json()

        if tipo == "numero":
            pedidos = [p for p in pedidos if str(p.get("PedidoOrigem")) == numero]

        if tipo == "escola":
            pedidos = [p for p in pedidos if escola in p["Escola"].lower()]

        if not pedidos:
            return jsonify({"text": "Nenhum pedido encontrado."}), 200

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

        return jsonify({"response_type": "in_channel", "text": resposta}), 200

    except Exception as e:
        return jsonify({"text": f"Erro: {str(e)}"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
