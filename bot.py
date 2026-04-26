"""
Bot MegaCrédito — Evolution API
- 18h: cobra inadimplentes via WhatsApp
- 23h: envia resumo do dia para o owner
- Webhook: lê comprovante (foto/PDF) e dá baixa automática
"""

import os, re, base64, requests
from datetime import date, datetime
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from google import genai
from google.genai import types

# ── Configurações ────────────────────────────────────────────────
EVOLUTION_URL      = os.environ.get("EVOLUTION_URL", "https://evolution-api-production-ddb3.up.railway.app")
EVOLUTION_KEY      = os.environ.get("EVOLUTION_KEY", "megacredito2025")
INSTANCE           = os.environ.get("EVOLUTION_INSTANCE", "MegaCrédito")
MEGACREDITO_URL    = os.environ.get("MEGACREDITO_URL", "https://wholesome-empathy-production-af46.up.railway.app")
MEGACREDITO_KEY    = os.environ.get("MEGACREDITO_KEY", "megacredito2025")
OWNER_NUMBER       = os.environ.get("OWNER_NUMBER", "558108071830883")
GEMINI_KEY         = os.environ.get("GEMINI_API_KEY", "")
BOT_SECRET         = os.environ.get("BOT_SECRET", "megabot2025")

app = Flask(__name__)

# ── Helpers Evolution API ────────────────────────────────────────

def headers():
    return {"apikey": EVOLUTION_KEY, "Content-Type": "application/json"}

def enviar_texto(numero: str, texto: str):
    """Envia mensagem de texto via Evolution API."""
    numero = re.sub(r'\D', '', numero)
    if not numero.startswith('55'):
        numero = '55' + numero
    url = f"{EVOLUTION_URL}/message/sendText/{INSTANCE}"
    payload = {"number": numero, "text": texto}
    print(f"[BOT] Enviando para {numero} | URL: {url}")
    try:
        r = requests.post(url, json=payload, headers=headers(), timeout=15)
        print(f"[BOT] Resposta Evolution: {r.status_code} | {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"[BOT] Erro ao enviar para {numero}: {e}")
        return False

def baixar_midia(message_id: str) -> bytes | None:
    """Baixa mídia (foto/PDF) de uma mensagem."""
    url = f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{INSTANCE}"
    try:
        r = requests.post(url, json={"message": {"key": {"id": message_id}}},
                          headers=headers(), timeout=30)
        if r.ok:
            data = r.json()
            b64 = data.get("base64", "")
            if b64:
                return base64.b64decode(b64)
    except Exception as e:
        print(f"[BOT] Erro ao baixar mídia: {e}")
    return None

# ── Helpers MegaCrédito API ──────────────────────────────────────

def get_inadimplentes():
    """Busca clientes inadimplentes no MegaCrédito."""
    try:
        r = requests.get(
            f"{MEGACREDITO_URL}/api/inadimplentes",
            headers={"X-API-Key": MEGACREDITO_KEY},
            timeout=10
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar inadimplentes: {e}")
    return []

def get_stats():
    """Busca estatísticas do dia."""
    try:
        r = requests.get(
            f"{MEGACREDITO_URL}/api/stats",
            headers={"X-API-Key": MEGACREDITO_KEY},
            timeout=10
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar stats: {e}")
    return {}

def registrar_pagamento(cliente_id: int, valor: float, obs: str = ""):
    """Dá baixa no pagamento via API do MegaCrédito."""
    try:
        r = requests.post(
            f"{MEGACREDITO_URL}/api/pagar/{cliente_id}",
            json={"valor": valor, "obs": obs},
            headers={"X-API-Key": MEGACREDITO_KEY},
            timeout=10
        )
        return r.ok
    except Exception as e:
        print(f"[BOT] Erro ao registrar pagamento: {e}")
    return False

def buscar_cliente_por_numero(numero: str):
    """Busca cliente pelo número de WhatsApp."""
    numero_limpo = re.sub(r'\D', '', numero)
    if numero_limpo.startswith('55') and len(numero_limpo) > 11:
        numero_limpo = numero_limpo[2:]
    try:
        r = requests.get(
            f"{MEGACREDITO_URL}/api/cliente_por_whatsapp/{numero_limpo}",
            headers={"X-API-Key": MEGACREDITO_KEY},
            timeout=10
        )
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar cliente: {e}")
    return None

# ── Leitura de Comprovante com IA ────────────────────────────────

def extrair_valor_comprovante(imagem_bytes: bytes, mime: str = "image/jpeg") -> float | None:
    """Usa Gemini Vision para extrair o valor do comprovante (imagem ou PDF)."""
    if not GEMINI_KEY:
        print("[BOT] GEMINI_API_KEY não configurada")
        return None
    try:
        client = genai.Client(api_key=GEMINI_KEY)

        prompt = (
            "Este é um comprovante de pagamento brasileiro. "
            "Extraia APENAS o valor total transferido/pago em reais. "
            "Responda SOMENTE com o número, sem R$, sem texto. "
            "Use ponto como separador decimal. "
            "Exemplo: 150.00"
        )

        response = client.models.generate_content(
            model="gemini-1.5-flash-latest",
            contents=[
                types.Part.from_bytes(data=imagem_bytes, mime_type=mime),
                prompt
            ]
        )

        texto = response.text.strip()
        texto = texto.replace(',', '.').replace('R$', '').strip()
        valor = float(re.search(r'[\d.]+', texto).group())
        return valor
    except Exception as e:
        print(f"[BOT] Erro ao extrair valor com Gemini: {e}")
        return None

# ── Jobs Agendados ───────────────────────────────────────────────

def job_cobranca_18h():
    """Envia cobranças para inadimplentes às 18h."""
    print(f"[BOT] {datetime.now()} — Iniciando cobrança 18h")
    inadimplentes = get_inadimplentes()
    enviados = 0
    for c in inadimplentes:
        if not c.get('whatsapp'):
            continue
        nome    = c['nome'].split()[0]
        dias    = c['dias_atraso']
        valor   = c['valor_atraso']
        diarias = c['diarias_pagas']
        msg = (
            f"Olá *{nome}*! 👋\n\n"
            f"Passando para lembrar que você está com *{dias} dia(s) em atraso* "
            f"no MegaCrédito.\n\n"
            f"💰 *Valor em aberto: R$ {valor:.2f}*\n"
            f"📊 Diárias pagas: {diarias}/20\n\n"
            f"Regularize hoje para evitar juros! 🙏\n"
            f"Qualquer dúvida é só responder aqui."
        )
        if enviar_texto(c['whatsapp'], msg):
            enviados += 1
    print(f"[BOT] Cobranças enviadas: {enviados}/{len(inadimplentes)}")

def job_resumo_23h():
    """Envia resumo do dia para o owner às 23h."""
    print(f"[BOT] {datetime.now()} — Enviando resumo para owner")
    stats         = get_stats()
    inadimplentes = get_inadimplentes()
    hoje          = date.today().strftime('%d/%m/%Y')
    total_hoje    = stats.get('total_hoje', 0)
    total_mes     = stats.get('total_mes', 0)
    em_atraso     = stats.get('em_atraso', 0)

    lista_inad = ""
    for c in inadimplentes[:15]:
        lista_inad += f"  • {c['nome']} — {c['dias_atraso']}d — R$ {c['valor_atraso']:.2f}\n"
    if not lista_inad:
        lista_inad = "  ✅ Nenhum inadimplente hoje!\n"

    msg = (
        f"📊 *RESUMO MEGACRÉDITO — {hoje}*\n"
        f"{'─'*30}\n\n"
        f"💵 *Recebido hoje:* R$ {total_hoje:.2f}\n"
        f"📅 *Recebido no mês:* R$ {total_mes:.2f}\n"
        f"⚠️ *Em atraso:* {em_atraso} cliente(s)\n\n"
        f"*📋 Lista de inadimplentes:*\n"
        f"{lista_inad}\n"
        f"Bom descanso! 🌙"
    )
    enviar_texto(OWNER_NUMBER, msg)

# ── Webhook — recebe mensagens ───────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    print(f"[BOT] Webhook recebido: {data}")

    evento = data.get('event', '')
    if evento not in ('messages.upsert', 'message.received', 'MESSAGES_UPSERT'):
        return jsonify(ok=True)

    msg_data   = data.get('data', {})
    key        = msg_data.get('key', {})

    if key.get('fromMe'):
        return jsonify(ok=True)

    remoteJid  = key.get('remoteJid', '')
    numero     = remoteJid.replace('@s.whatsapp.net', '')
    message    = msg_data.get('message', {})
    message_id = key.get('id', '')

    print(f"[BOT] Mensagem de {numero} | tipo: {list(message.keys())}")

    tem_imagem = 'imageMessage' in message
    tem_pdf    = ('documentMessage' in message and
                  'pdf' in (message.get('documentMessage', {}).get('mimetype', '')))

    if tem_imagem or tem_pdf:
        mime  = "image/jpeg" if tem_imagem else "application/pdf"
        midia = baixar_midia(message_id)
        if not midia:
            enviar_texto(numero, "❌ Não consegui baixar o arquivo. Tente novamente.")
            return jsonify(ok=True)

        valor = extrair_valor_comprovante(midia, mime)
        if not valor:
            enviar_texto(numero, "❌ Não consegui ler o valor do comprovante. Manda uma foto mais nítida.")
            return jsonify(ok=True)

        cliente = buscar_cliente_por_numero(numero)
        if not cliente:
            enviar_texto(numero,
                f"✅ Comprovante recebido! Valor: R$ {valor:.2f}\n\n"
                f"⚠️ Não encontrei seu cadastro. Fale com o atendente."
            )
            return jsonify(ok=True)

        ok = registrar_pagamento(cliente['id'], valor, obs="Comprovante via WhatsApp")
        nome = cliente['nome'].split()[0]
        if ok:
            enviar_texto(numero,
                f"✅ *Pagamento confirmado, {nome}!*\n\n"
                f"💰 Valor: R$ {valor:.2f}\n"
                f"📊 Suas diárias foram atualizadas!\n\n"
                f"Obrigado! 🙏"
            )
            enviar_texto(OWNER_NUMBER,
                f"💰 *Pagamento recebido!*\n"
                f"Cliente: {cliente['nome']}\n"
                f"Valor: R$ {valor:.2f}\n"
                f"Via: Comprovante WhatsApp"
            )
        else:
            enviar_texto(numero,
                f"⚠️ Comprovante recebido (R$ {valor:.2f}), mas ocorreu um erro ao registrar. "
                f"Fale com o atendente."
            )

    elif 'conversation' in message or 'extendedTextMessage' in message:
        texto = (message.get('conversation') or
                 message.get('extendedTextMessage', {}).get('text', '')).lower().strip()

        print(f"[BOT] Texto recebido: '{texto}'")

        if any(p in texto for p in ['oi', 'olá', 'ola', 'bom dia', 'boa tarde', 'boa noite']):
            cliente = buscar_cliente_por_numero(numero)
            nome    = cliente['nome'].split()[0] if cliente else "cliente"
            enviar_texto(numero,
                f"Olá *{nome}*! 👋\n\n"
                f"Sou o assistente do *MegaCrédito*.\n\n"
                f"📎 Para pagar, envie a foto ou PDF do seu comprovante aqui.\n"
                f"📊 Para ver seu saldo, digite *saldo*.\n"
                f"❓ Para falar com atendente, digite *atendente*."
            )

        elif 'saldo' in texto:
            cliente = buscar_cliente_por_numero(numero)
            if cliente:
                enviar_texto(numero,
                    f"📊 *Seu saldo, {cliente['nome'].split()[0]}:*\n\n"
                    f"✅ Diárias pagas: {cliente['diarias_pagas']}/20\n"
                    f"💰 Total pago: R$ {cliente['total_pago']:.2f}\n"
                    f"⚠️ Em atraso: {cliente['dias_em_atraso']} dia(s)\n"
                    f"💸 Valor em aberto: R$ {cliente['valor_em_atraso']:.2f}"
                )
            else:
                enviar_texto(numero, "❌ Não encontrei seu cadastro. Fale com o atendente.")

        elif 'atendente' in texto or 'humano' in texto:
            enviar_texto(numero, "👤 Aguarde, vou chamar o atendente...")
            enviar_texto(OWNER_NUMBER,
                f"🔔 *Cliente quer falar com atendente!*\n"
                f"Número: +{numero}\n"
                f"Hora: {datetime.now().strftime('%H:%M')}"
            )

    return jsonify(ok=True)

# ── Rotas de teste / disparo manual ─────────────────────────────

@app.route('/disparar/cobranca', methods=['POST'])
def disparar_cobranca():
    if request.headers.get('X-Secret') != BOT_SECRET:
        return jsonify(erro="não autorizado"), 403
    job_cobranca_18h()
    return jsonify(ok=True, msg="Cobranças disparadas")

@app.route('/disparar/resumo', methods=['POST'])
def disparar_resumo():
    if request.headers.get('X-Secret') != BOT_SECRET:
        return jsonify(erro="não autorizado"), 403
    job_resumo_23h()
    return jsonify(ok=True, msg="Resumo enviado")

@app.route('/health')
def health():
    return jsonify(status="ok", hora=datetime.now().isoformat())

# ── Inicialização ────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone="America/Fortaleza")
scheduler.add_job(job_cobranca_18h, 'cron', hour=18, minute=0)
scheduler.add_job(job_resumo_23h,   'cron', hour=23, minute=0)
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"[BOT] Iniciando na porta {port}")
    app.run(host='0.0.0.0', port=port)
