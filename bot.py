"""
Bot MegaCrédito — Evolution API
- 18h: cobra inadimplentes via WhatsApp
- 23h: envia resumo do dia para o owner
- Webhook: lê comprovante (foto/PDF) e dá baixa automática
- Antifraude: cruza comprovantes com extrato bancário enviado pelo owner
"""

import os, re, base64, requests, json
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI

# ── Configurações ────────────────────────────────────────────────
EVOLUTION_URL   = os.environ.get("EVOLUTION_URL", "https://evolution-api-production-ddb3.up.railway.app")
EVOLUTION_KEY   = os.environ.get("EVOLUTION_KEY", "megacredito2025")
INSTANCE        = os.environ.get("EVOLUTION_INSTANCE", "MegaCrédito")
MEGACREDITO_URL = os.environ.get("MEGACREDITO_URL", "https://wholesome-empathy-production-af46.up.railway.app")
MEGACREDITO_KEY = os.environ.get("MEGACREDITO_KEY", "megacredito2025")
OWNER_NUMBER    = os.environ.get("OWNER_NUMBER", "8108071830883")
FUNC_NUMBER     = os.environ.get("FUNC_NUMBER", "")   # ← adicionar no Railway quando tiver o número
OPENAI_KEY      = os.environ.get("OPENAI_API_KEY", "")
BOT_SECRET      = os.environ.get("BOT_SECRET", "megabot2025")

app = Flask(__name__)

# ── Armazenamento temporário de comprovantes do dia ─────────────
# { "2026-04-26": [ {cliente_id, nome, valor, hora_str, pag_id}, ... ] }
comprovantes_dia: dict = {}

# ── Helpers Evolution API ────────────────────────────────────────

def headers():
    return {"apikey": EVOLUTION_KEY, "Content-Type": "application/json"}

def enviar_texto(numero: str, texto: str):
    if not numero:
        return False
    numero = re.sub(r'\D', '', numero)
    if not numero.startswith('55'):
        numero = '55' + numero
    url = f"{EVOLUTION_URL}/message/sendText/{INSTANCE}"
    try:
        r = requests.post(url, json={"number": numero, "text": texto}, headers=headers(), timeout=15)
        return r.ok
    except Exception as e:
        print(f"[BOT] Erro ao enviar para {numero}: {e}")
        return False

def enviar_admins(texto: str):
    """Envia mensagem para o owner e para a funcionária (se cadastrada)."""
    enviar_texto(OWNER_NUMBER, texto)
    if FUNC_NUMBER:
        enviar_texto(FUNC_NUMBER, texto)

def baixar_midia(message_id: str) -> bytes | None:
    url = f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{INSTANCE}"
    try:
        r = requests.post(url, json={"message": {"key": {"id": message_id}}},
                          headers=headers(), timeout=30)
        print(f"[BOT] baixar_midia status: {r.status_code}")
        if r.ok:
            data = r.json()
            b64 = data.get("base64", "")
            print(f"[BOT] base64 recebido: {len(b64)} chars")
            if b64:
                return base64.b64decode(b64)
    except Exception as e:
        print(f"[BOT] Erro ao baixar mídia: {e}")
    return None

# ── Helpers MegaCrédito API ──────────────────────────────────────

def get_inadimplentes():
    try:
        r = requests.get(f"{MEGACREDITO_URL}/api/inadimplentes",
                         headers={"X-API-Key": MEGACREDITO_KEY}, timeout=10)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar inadimplentes: {e}")
    return []

def get_stats():
    try:
        r = requests.get(f"{MEGACREDITO_URL}/api/stats",
                         headers={"X-API-Key": MEGACREDITO_KEY}, timeout=10)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar stats: {e}")
    return {}

def registrar_pagamento_retorno(cliente_id: int, valor: float, obs: str = ""):
    try:
        r = requests.post(f"{MEGACREDITO_URL}/api/pagar/{cliente_id}",
                          json={"valor": valor, "obs": obs},
                          headers={"X-API-Key": MEGACREDITO_KEY}, timeout=10)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao registrar pagamento: {e}")
    return None

def reverter_pagamento(pag_id: int):
    """Reverte um pagamento fraudulento."""
    try:
        r = requests.post(f"{MEGACREDITO_URL}/api/reverter/{pag_id}",
                          headers={"X-API-Key": MEGACREDITO_KEY}, timeout=10)
        return r.ok
    except Exception as e:
        print(f"[BOT] Erro ao reverter pagamento: {e}")
    return False

def buscar_cliente_por_numero(numero: str):
    numero_limpo = re.sub(r'\D', '', numero)
    if numero_limpo.startswith('55') and len(numero_limpo) > 11:
        numero_limpo = numero_limpo[2:]
    try:
        r = requests.get(f"{MEGACREDITO_URL}/api/cliente_por_whatsapp/{numero_limpo}",
                         headers={"X-API-Key": MEGACREDITO_KEY}, timeout=10)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar cliente: {e}")
    return None

def pagou_hoje(cliente_id: int) -> bool:
    try:
        r = requests.get(f"{MEGACREDITO_URL}/api/pagamentos_hoje/{cliente_id}",
                         headers={"X-API-Key": MEGACREDITO_KEY}, timeout=10)
        if r.ok:
            return r.json().get('pagou_hoje', False)
    except Exception as e:
        print(f"[BOT] Erro ao checar pagamento hoje: {e}")
    return False

# ── Helpers de texto ─────────────────────────────────────────────

def gerar_aviso_dias_atraso(dias: int) -> str:
    hoje = date.today()
    dias_lista = [(hoje - timedelta(days=i)).strftime('%d/%m') for i in range(dias, 0, -1)]
    if len(dias_lista) == 1:
        return f"⚠️ Dia em atraso: *{dias_lista[0]}*"
    return f"⚠️ Dias em atraso: *{', '.join(dias_lista)}*"

# ── Leitura de imagem/PDF com GPT-4o ────────────────────────────

def extrair_valor_comprovante(imagem_bytes: bytes, mime: str = "image/jpeg") -> float | None:
    if not OPENAI_KEY:
        print("[BOT] OPENAI_API_KEY não configurada")
        return None
    try:
        client = OpenAI(api_key=OPENAI_KEY)
        b64 = base64.b64encode(imagem_bytes).decode("utf-8")
        data_uri = f"data:{mime};base64,{b64}"
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=100,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "low"}},
                {"type": "text", "text": (
                    "Este é um comprovante de pagamento/transferência brasileiro (PIX, TED, DOC ou boleto).\n"
                    "Sua tarefa: encontrar o VALOR DA TRANSFERÊNCIA ou VALOR PAGO.\n\n"
                    "REGRAS IMPORTANTES:\n"
                    "- Procure por campos como Valor, Valor da transferência, Valor pago, Quantia\n"
                    "- IGNORE completamente: números de agência, conta, CPF, CNPJ, datas e códigos\n"
                    "- O valor geralmente aparece em destaque, precedido de R$\n"
                    "- Se houver Valor e Tarifa separados, retorne apenas o Valor principal\n\n"
                    "Responda SOMENTE com o número em reais, sem R$, sem texto.\n"
                    "Use ponto como separador decimal.\n"
                    "Exemplos de resposta correta: 4.00 / 150.50 / 1200.00"
                )},
            ]}],
        )
        texto = response.choices[0].message.content.strip()
        print(f"[BOT] GPT-4o valor: {texto}")
        texto = texto.replace(',', '.').replace('R$', '').strip()
        return float(re.search(r'[\d.]+', texto).group())
    except Exception as e:
        print(f"[BOT] Erro ao extrair valor com GPT-4o: {e}")
        return None

def extrair_hora_comprovante(imagem_bytes: bytes, mime: str = "image/jpeg") -> str | None:
    """Extrai o horário do comprovante no formato HH:MM."""
    if not OPENAI_KEY:
        return None
    try:
        client = OpenAI(api_key=OPENAI_KEY)
        b64 = base64.b64encode(imagem_bytes).decode("utf-8")
        data_uri = f"data:{mime};base64,{b64}"
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=20,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "low"}},
                {"type": "text", "text": (
                    "Este é um comprovante de pagamento brasileiro.\n"
                    "Extraia APENAS o horário da transação.\n"
                    "Responda SOMENTE no formato HH:MM, sem texto adicional.\n"
                    "Exemplo: 14:32"
                )},
            ]}],
        )
        texto = response.choices[0].message.content.strip()
        print(f"[BOT] GPT-4o hora: {texto}")
        match = re.search(r'\d{1,2}:\d{2}', texto)
        if match:
            return match.group()
    except Exception as e:
        print(f"[BOT] Erro ao extrair hora: {e}")
    return None

def extrair_transacoes_extrato(pdf_bytes: bytes) -> list:
    """
    Lê o extrato PDF e retorna lista de transações:
    [ {valor: float, hora: str, nome: str}, ... ]
    """
    if not OPENAI_KEY:
        return []
    try:
        client = OpenAI(api_key=OPENAI_KEY)
        b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        data_uri = f"data:application/pdf;base64,{b64}"
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=2000,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                {"type": "text", "text": (
                    "Este é um extrato bancário brasileiro.\n"
                    "Liste TODAS as entradas de dinheiro (PIX recebido, TED recebida, depósitos).\n"
                    "Para cada transação retorne um JSON com: valor (número), hora (HH:MM), nome (remetente se disponível, senão 'Desconhecido').\n"
                    "Retorne APENAS um array JSON válido, sem texto, sem explicações, sem markdown.\n"
                    "Exemplo: [{\"valor\": 150.00, \"hora\": \"14:32\", \"nome\": \"João Silva\"}, ...]"
                )},
            ]}],
        )
        texto = response.choices[0].message.content.strip()
        texto = re.sub(r'```json|```', '', texto).strip()
        print(f"[BOT] Extrato extraído: {texto[:300]}")
        return json.loads(texto)
    except Exception as e:
        print(f"[BOT] Erro ao extrair extrato: {e}")
    return []

# ── Antifraude ───────────────────────────────────────────────────

def hora_para_minutos(hora_str: str) -> int | None:
    """Converte HH:MM para total de minutos."""
    try:
        h, m = hora_str.strip().split(':')
        return int(h) * 60 + int(m)
    except:
        return None

def verificar_fraudes(transacoes_extrato: list):
    """
    Cruza comprovantes do dia com o extrato.
    Tolerância: 1 minuto.
    """
    hoje = date.today().isoformat()
    comprovantes = comprovantes_dia.get(hoje, [])

    if not comprovantes:
        enviar_texto(OWNER_NUMBER, "✅ Extrato processado. Nenhum comprovante recebido hoje para verificar.")
        return

    fraudes    = []
    confirmados = 0

    for comp in comprovantes:
        valor_comp = comp['valor']
        hora_comp  = comp.get('hora')
        nome_comp  = comp['nome']
        pag_id     = comp['pag_id']

        min_comp = hora_para_minutos(hora_comp) if hora_comp else None
        encontrou = False

        for tx in transacoes_extrato:
            valor_tx = float(tx.get('valor', 0))
            hora_tx  = tx.get('hora', '')
            min_tx   = hora_para_minutos(hora_tx)

            # Verifica valor igual
            if abs(valor_tx - valor_comp) > 0.01:
                continue

            # Verifica horário (tolerância 1 minuto)
            if min_comp is not None and min_tx is not None:
                if abs(min_tx - min_comp) <= 1:
                    encontrou = True
                    break
            else:
                # Se não tem hora no comprovante, aceita pelo valor
                encontrou = True
                break

        if encontrou:
            confirmados += 1
        else:
            fraudes.append({
                'nome':   nome_comp,
                'valor':  valor_comp,
                'hora':   hora_comp or '??:??',
                'pag_id': pag_id,
                'cliente_id': comp['cliente_id']
            })

    # Envia resumo
    msg_resumo = (
        f"🔍 *VERIFICAÇÃO ANTIFRAUDE — {date.today().strftime('%d/%m/%Y')}*\n"
        f"{'─'*30}\n\n"
        f"✅ Confirmados: {confirmados}\n"
        f"🚨 Suspeitos: {len(fraudes)}\n"
    )
    enviar_admins(msg_resumo)

    # Processa fraudes
    for f in fraudes:
        # Reverte pagamento
        revertido = reverter_pagamento(f['pag_id'])
        status    = "✅ Revertido automaticamente" if revertido else "⚠️ Reversão falhou — verifique manualmente"

        alerta = (
            f"🚨 *PAGAMENTO SUSPEITO!*\n\n"
            f"👤 Cliente: *{f['nome']}*\n"
            f"💰 Valor: R$ {f['valor']:.2f}\n"
            f"🕐 Horário do comprovante: {f['hora']}\n\n"
            f"❌ Nenhuma entrada correspondente no extrato!\n"
            f"📋 {status}\n\n"
            f"Faça a verificação manual se necessário."
        )
        enviar_admins(alerta)

    if not fraudes:
        enviar_admins("🎉 Todos os pagamentos do dia foram confirmados no extrato!")

# ── Jobs Agendados ───────────────────────────────────────────────

def job_cobranca_18h():
    print(f"[BOT] {datetime.now()} — Iniciando cobrança 18h")
    inadimplentes = get_inadimplentes()
    enviados = 0
    pulados  = 0
    for c in inadimplentes:
        if not c.get('whatsapp'):
            continue
        if pagou_hoje(c['id']):
            pulados += 1
            continue
        nome    = c['nome'].split()[0]
        dias    = c['dias_atraso']
        valor   = c['valor_atraso']
        diarias = c['diarias_pagas']
        aviso   = gerar_aviso_dias_atraso(dias)
        msg = (
            f"Olá *{nome}*! 👋\n\n"
            f"Passando para lembrar que você está com *{dias} dia(s) em atraso* no MegaCrédito.\n\n"
            f"{aviso}\n"
            f"💰 *Valor em aberto: R$ {valor:.2f}*\n"
            f"📊 Diárias pagas: {diarias}/20\n\n"
            f"Regularize hoje para evitar juros! 🙏\n"
            f"Qualquer dúvida é só responder aqui."
        )
        if enviar_texto(c['whatsapp'], msg):
            enviados += 1
    print(f"[BOT] Cobranças enviadas: {enviados} | Pulados (pagaram hoje): {pulados}")

def job_resumo_23h():
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

# ── Webhook ──────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    print(f"[BOT] PAYLOAD: {json.dumps(data, ensure_ascii=False)[:500]}")

    evento = data.get('event', '')
    if evento not in ('messages.upsert', 'message.received'):
        return jsonify(ok=True)

    msg_data   = data.get('data', {})
    key        = msg_data.get('key', {})
    if key.get('fromMe'):
        return jsonify(ok=True)

    remoteJid  = key.get('remoteJid', '')
    numero     = remoteJid.replace('@s.whatsapp.net', '')
    message    = msg_data.get('message', {})
    message_id = key.get('id', '')

    print(f"[BOT] De: {numero} | Chaves: {list(message.keys())}")

    tem_imagem = 'imageMessage' in message
    tem_pdf    = ('documentMessage' in message and
                  'pdf' in (message.get('documentMessage', {}).get('mimetype', '')))

    # ── Owner enviou extrato bancário ────────────────────────────
    numero_limpo = re.sub(r'\D', '', numero)
    owner_limpo  = re.sub(r'\D', '', OWNER_NUMBER)
    is_owner     = numero_limpo.endswith(owner_limpo[-8:])

    if tem_pdf and is_owner:
        enviar_texto(OWNER_NUMBER, "📊 Extrato recebido! Processando verificação antifraude... aguarde.")
        midia = baixar_midia(message_id)
        if not midia:
            enviar_texto(OWNER_NUMBER, "❌ Não consegui baixar o extrato. Tente novamente.")
            return jsonify(ok=True)
        transacoes = extrair_transacoes_extrato(midia)
        print(f"[BOT] Transações extraídas: {len(transacoes)}")
        verificar_fraudes(transacoes)
        return jsonify(ok=True)

    # ── Cliente enviou comprovante ───────────────────────────────
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

        hora  = extrair_hora_comprovante(midia, mime)

        cliente = buscar_cliente_por_numero(numero)
        if not cliente:
            enviar_texto(numero,
                f"✅ Comprovante recebido! Valor: R$ {valor:.2f}\n\n"
                f"⚠️ Não encontrei seu cadastro. Fale com o atendente."
            )
            return jsonify(ok=True)

        resultado = registrar_pagamento_retorno(cliente['id'], valor, obs="Comprovante via WhatsApp")
        nome = cliente['nome'].split()[0]

        if resultado:
            pag_id = resultado.get('pag_id')

            # Salva comprovante para verificação antifraude
            hoje = date.today().isoformat()
            if hoje not in comprovantes_dia:
                comprovantes_dia[hoje] = []
            comprovantes_dia[hoje].append({
                'cliente_id': cliente['id'],
                'nome':       cliente['nome'],
                'valor':      valor,
                'hora':       hora,
                'pag_id':     pag_id,
            })

            diarias_pagas = resultado.get('diarias_pagas', cliente['diarias_pagas'])
            diarias_novas = resultado.get('diarias_novas', 0)
            restantes     = 20 - diarias_pagas

            if diarias_pagas >= 20:
                msg_parcelas = "🎉 *Parabéns! Você completou todas as 20 diárias!*\nAguarde a renovação do contrato."
            elif diarias_novas == 0:
                msg_parcelas = "⏳ Pagamento parcial registrado. Continue pagando para completar a próxima diária."
            else:
                msg_parcelas = (
                    f"📊 *{diarias_pagas}/20 diárias pagas*\n"
                    f"✅ +{diarias_novas} diária(s) neste pagamento\n"
                    f"📅 Faltam {restantes} diária(s) para concluir"
                )

            dias_restantes = resultado.get('dias_em_atraso', 0)
            aviso_atraso   = ""
            if dias_restantes > 0:
                aviso_atraso = "\n\n" + gerar_aviso_dias_atraso(dias_restantes) + \
                               f" ainda em aberto\n💸 Valor pendente: R$ {resultado.get('valor_em_atraso', 0):.2f}"

            enviar_texto(numero,
                f"✅ *Pagamento confirmado, {nome}!*\n\n"
                f"💰 Valor: R$ {valor:.2f}\n\n"
                f"{msg_parcelas}"
                f"{aviso_atraso}\n\n"
                f"Obrigado! 🙏"
            )
            enviar_admins(
                f"💰 *Pagamento recebido!*\n"
                f"Cliente: {cliente['nome']}\n"
                f"Valor: R$ {valor:.2f}\n"
                f"Hora: {hora or '??:??'}\n"
                f"Diárias: {diarias_pagas}/20\n"
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
            enviar_admins(
                f"🔔 *Cliente quer falar com atendente!*\n"
                f"Número: +{numero}\n"
                f"Hora: {datetime.now().strftime('%H:%M')}"
            )

    return jsonify(ok=True)

# ── Rotas manuais ────────────────────────────────────────────────

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
