"""
Bot MegaCrédito — Evolution API
- 18h: cobra inadimplentes que não pagaram hoje
- 23h: resumo do dia só para o owner
- 23h50: backup de todos os ativos para owner e funcionária
- Webhook: lê comprovante com antifraude completo
- Antifraude: hash SHA-256 + código TX + nome + extrato bancário
"""

import os, re, base64, requests, json, hashlib, threading
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

# ── Armazenamento em memória dos comprovantes do dia (antifraude extrato) ──
# { "2026-04-28": [ {cliente_id, nome, valor, hora, pag_id}, ... ] }
comprovantes_dia: dict = {}

# ── Helpers Evolution API ────────────────────────────────────────

def headers():
    return {"apikey": EVOLUTION_KEY, "Content-Type": "application/json"}

def enviar_texto(numero: str, texto: str) -> bool:
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

def enviar_alerta_admins(texto: str):
    """Envia APENAS para alertas de problema — owner e funcionária."""
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

def _api_headers():
    return {"X-API-Key": MEGACREDITO_KEY}

def get_inadimplentes():
    try:
        r = requests.get(f"{MEGACREDITO_URL}/api/inadimplentes", headers=_api_headers(), timeout=10)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar inadimplentes: {e}")
    return []

def get_stats():
    try:
        r = requests.get(f"{MEGACREDITO_URL}/api/stats", headers=_api_headers(), timeout=10)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar stats: {e}")
    return {}

def get_clientes_ativos():
    try:
        r = requests.get(f"{MEGACREDITO_URL}/api/clientes_ativos", headers=_api_headers(), timeout=10)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar clientes ativos: {e}")
    return []

def buscar_cliente_por_numero(numero: str):
    numero_limpo = re.sub(r'\D', '', numero)
    if numero_limpo.startswith('55') and len(numero_limpo) > 11:
        numero_limpo = numero_limpo[2:]
    try:
        r = requests.get(f"{MEGACREDITO_URL}/api/cliente_por_whatsapp/{numero_limpo}",
                         headers=_api_headers(), timeout=10)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao buscar cliente: {e}")
    return None

def pagou_hoje(cliente_id: int) -> bool:
    try:
        r = requests.get(f"{MEGACREDITO_URL}/api/pagamentos_hoje/{cliente_id}",
                         headers=_api_headers(), timeout=10)
        if r.ok:
            return r.json().get('pagou_hoje', False)
    except Exception as e:
        print(f"[BOT] Erro ao checar pagamento hoje: {e}")
    return False

def verificar_duplicado_api(hash_arquivo: str, codigo_tx: str) -> tuple[bool, str]:
    """Consulta a API para verificar duplicata pelo codigo_tx (ID único do PIX)."""
    if not codigo_tx:
        return False, ''
    try:
        r = requests.post(f"{MEGACREDITO_URL}/api/verificar_comprovante",
                          json={"codigo_tx": codigo_tx},
                          headers=_api_headers(), timeout=10)
        if r.ok:
            data = r.json()
            return data.get('duplicado', False), data.get('motivo', '')
    except Exception as e:
        print(f"[BOT] Erro ao verificar duplicado: {e}")
    return False, ''

def registrar_pagamento(cliente_id: int, valor: float, obs: str,
                        hash_arquivo: str, codigo_tx: str):
    try:
        r = requests.post(f"{MEGACREDITO_URL}/api/pagar/{cliente_id}",
                          json={"valor": valor, "obs": obs,
                                "hash_arquivo": hash_arquivo, "codigo_tx": codigo_tx},
                          headers=_api_headers(), timeout=10)
        print(f"[BOT] registrar_pagamento status={r.status_code} body={r.text[:300]}")
        if r.ok:
            return r.json()
        else:
            print(f"[BOT] ERRO ao registrar: {r.status_code} — {r.text}")
    except Exception as e:
        print(f"[BOT] Erro ao registrar pagamento: {e}")
    return None

def upsert_clientes(clientes: list) -> dict:
    """Envia lista de backup para o site cadastrar/atualizar clientes."""
    try:
        r = requests.post(f"{MEGACREDITO_URL}/api/upsert_clientes",
                          json={"clientes": clientes},
                          headers=_api_headers(), timeout=30)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[BOT] Erro ao upsert_clientes: {e}")
    return {}


def reverter_pagamento(pag_id: int) -> bool:
    """Reverte um pagamento pelo ID — usado no antifraude."""
    try:
        r = requests.post(f"{MEGACREDITO_URL}/api/reverter/{pag_id}",
                          headers=_api_headers(), timeout=10)
        return r.ok
    except Exception as e:
        print(f"[BOT] Erro ao reverter pagamento: {e}")
    return False

# ── Helpers de texto ─────────────────────────────────────────────

def gerar_aviso_dias_atraso(dias: int) -> str:
    hoje      = date.today()
    dias_lista = [(hoje - timedelta(days=i)).strftime('%d/%m') for i in range(dias, 0, -1)]
    if len(dias_lista) == 1:
        return f"⚠️ Dia em atraso: *{dias_lista[0]}*"
    return f"⚠️ Dias em atraso: *{', '.join(dias_lista)}*"

def hora_para_minutos(hora_str: str) -> int | None:
    try:
        h, m = hora_str.strip().split(':')
        return int(h) * 60 + int(m)
    except:
        return None

# ── GPT-4o: extração unificada do comprovante ────────────────────

def pdf_para_imagem(pdf_bytes: bytes) -> bytes | None:
    """Converte a primeira página do PDF em JPEG para enviar ao GPT-4o Vision."""
    try:
        from pdf2image import convert_from_bytes
        from io import BytesIO
        paginas = convert_from_bytes(pdf_bytes, dpi=150, first_page=1, last_page=1)
        if not paginas:
            return None
        buf = BytesIO()
        paginas[0].save(buf, format='JPEG', quality=85)
        return buf.getvalue()
    except ImportError:
        print("[BOT] pdf2image não disponível — tentando via pypdf texto")
        return None
    except Exception as e:
        print(f"[BOT] Erro ao converter PDF para imagem: {e}")
        return None


def extrair_dados_comprovante(imagem_bytes: bytes, mime: str = "image/jpeg") -> dict | None:
    """
    Extrai valor, hora, nome_remetente e codigo_tx do comprovante.
    PDFs são convertidos para imagem antes de enviar ao GPT-4o (Vision só aceita imagens).
    Retorna: {valor, hora, nome_remetente, codigo_tx} ou None se falhar.
    """
    if not OPENAI_KEY:
        print("[BOT] OPENAI_API_KEY não configurada")
        return None

    # GPT-4o Vision não aceita PDF — converte para JPEG primeiro
    if 'pdf' in mime.lower():
        imagem_bytes = pdf_para_imagem(imagem_bytes)
        if not imagem_bytes:
            print("[BOT] Falha ao converter PDF para imagem")
            return None
        mime = "image/jpeg"

    try:
        client   = OpenAI(api_key=OPENAI_KEY)
        b64      = base64.b64encode(imagem_bytes).decode("utf-8")
        data_uri = f"data:{mime};base64,{b64}"

        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=300,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                {"type": "text", "text": (
                    "Este é um comprovante de pagamento/transferência brasileiro (PIX, TED, DOC ou boleto).\n"
                    "Extraia as 4 informações abaixo e retorne APENAS um JSON válido, sem markdown, sem texto extra.\n\n"
                    "Campos:\n"
                    "- valor: número decimal (ex: 150.00) — o valor transferido/pago. IGNORE agência, conta, CPF, CNPJ.\n"
                    "- hora: string HH:MM — horário da transação. Se não encontrar, use null.\n"
                    "- nome_remetente: string — nome de QUEM ENVIOU o dinheiro (não o favorecido/destinatário). Se não encontrar, use null.\n"
                    "- codigo_tx: string — código E2E, EndToEnd ID, TxID, NSU ou ID da transação. Se não encontrar, use null.\n\n"
                    "Exemplo de resposta:\n"
                    "{\"valor\": 150.00, \"hora\": \"14:32\", \"nome_remetente\": \"João Silva\", \"codigo_tx\": \"E60701554202604281432\"}"
                )},
            ]}],
        )
        texto = response.choices[0].message.content.strip()
        texto = re.sub(r'```json|```', '', texto).strip()
        print(f"[BOT] GPT-4o comprovante: {texto}")
        return json.loads(texto)
    except Exception as e:
        print(f"[BOT] Erro ao extrair dados do comprovante: {e}")
    return None

def extrair_transacoes_extrato(pdf_bytes: bytes) -> list:
    """Lê extrato PDF e retorna lista de {valor, hora, nome}."""
    if not OPENAI_KEY:
        return []
    try:
        client   = OpenAI(api_key=OPENAI_KEY)
        b64      = base64.b64encode(pdf_bytes).decode("utf-8")
        data_uri = f"data:application/pdf;base64,{b64}"
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=2000,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                {"type": "text", "text": (
                    "Este é um extrato bancário brasileiro.\n"
                    "Liste TODAS as entradas de dinheiro (PIX recebido, TED recebida, depósitos).\n"
                    "Retorne APENAS um array JSON válido, sem markdown, sem texto extra.\n"
                    "Cada item: {\"valor\": número, \"hora\": \"HH:MM\", \"nome\": \"remetente ou Desconhecido\"}\n"
                    "Exemplo: [{\"valor\": 150.00, \"hora\": \"14:32\", \"nome\": \"João Silva\"}]"
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

# ── Antifraude: cruzamento com extrato ──────────────────────────

def verificar_fraudes(transacoes_extrato: list):
    hoje         = date.today().isoformat()
    comprovantes = comprovantes_dia.get(hoje, [])

    if not comprovantes:
        enviar_texto(OWNER_NUMBER, "✅ Extrato processado. Nenhum comprovante recebido hoje para verificar.")
        return

    fraudes     = []
    confirmados = 0

    for comp in comprovantes:
        valor_comp = comp['valor']
        hora_comp  = comp.get('hora')
        min_comp   = hora_para_minutos(hora_comp) if hora_comp else None
        encontrou  = False

        for tx in transacoes_extrato:
            valor_tx = float(tx.get('valor', 0))
            min_tx   = hora_para_minutos(tx.get('hora', ''))

            if abs(valor_tx - valor_comp) > 0.01:
                continue
            if min_comp is not None and min_tx is not None:
                if abs(min_tx - min_comp) <= 1:
                    encontrou = True
                    break
            else:
                encontrou = True
                break

        if encontrou:
            confirmados += 1
        else:
            fraudes.append(comp)

    msg_resumo = (
        f"🔍 *VERIFICAÇÃO ANTIFRAUDE — {date.today().strftime('%d/%m/%Y')}*\n"
        f"{'─'*30}\n\n"
        f"✅ Confirmados: {confirmados}\n"
        f"🚨 Suspeitos: {len(fraudes)}\n"
    )
    enviar_alerta_admins(msg_resumo)

    for f in fraudes:
        revertido = reverter_pagamento(f['pag_id'])
        status    = "✅ Revertido automaticamente" if revertido else "⚠️ Reversão falhou — verifique manualmente"
        alerta = (
            f"🚨 *PAGAMENTO SUSPEITO!*\n\n"
            f"👤 Cliente: *{f['nome']}*\n"
            f"💰 Valor: R$ {f['valor']:.2f}\n"
            f"🕐 Horário do comprovante: {f.get('hora') or '??:??'}\n\n"
            f"❌ Nenhuma entrada correspondente no extrato!\n"
            f"📋 {status}\n\n"
            f"Faça a verificação manual se necessário."
        )
        enviar_alerta_admins(alerta)

    if not fraudes:
        enviar_alerta_admins("🎉 Todos os pagamentos do dia foram confirmados no extrato!")

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
        tipo    = c.get('tipo', 'diaria')
        aviso   = gerar_aviso_dias_atraso(dias)

        if tipo == 'mensalidade':
            msg = (
                f"Olá *{nome}*! 👋\n\n"
                f"Passando para lembrar que sua mensalidade está em atraso há *{dias} dia(s)*.\n\n"
                f"{aviso}\n"
                f"💰 *Valor em aberto: R$ {valor:.2f}*\n\n"
                f"Regularize hoje para evitar juros! 🙏\n"
                f"Qualquer dúvida é só responder aqui."
            )
        else:
            diarias = c['diarias_pagas']
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
    """Resumo financeiro — só para o owner."""
    print(f"[BOT] {datetime.now()} — Enviando resumo para owner")
    stats         = get_stats()
    inadimplentes = get_inadimplentes()
    hoje          = date.today().strftime('%d/%m/%Y')
    total_hoje    = stats.get('total_hoje', 0)
    total_mes     = stats.get('total_mes', 0)
    em_atraso     = stats.get('em_atraso', 0)
    lista_inad    = ""
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

def gerar_backup_completo(clientes: list) -> str:
    """
    Gera a mensagem de backup com TODOS os dados de cada cliente.
    Formato estruturado para ser reconhecido como backup pelo #atualizeiosite.
    """
    hoje      = date.today().strftime('%d/%m/%Y')
    em_dia    = 0
    em_atraso = 0
    aguardando = 0
    linhas    = []

    for c in clientes:
        nome        = c['nome']
        diarias     = c['diarias_pagas']
        atraso      = c['dias_em_atraso']
        saldo       = c['saldo_pendente']
        status      = c['status']
        whatsapp    = c.get('whatsapp', '')
        cpf         = c.get('cpf', '')
        valor_diaria = c.get('valor_diaria', 0)
        data_inicio = c.get('data_inicio', '')
        endereco    = c.get('endereco', '')
        email       = c.get('email', '')
        chave_pix   = c.get('chave_pix', '')
        limite      = c.get('limite', 0)

        if status == 'aguardando':
            icone = "⭐"; aguardando += 1
        elif atraso > 0:
            icone = "🔴"; em_atraso += 1
        else:
            icone = "🟢" if saldo == 0 else "🟡"; em_dia += 1

        # Linha principal
        linha = (
            f"{icone} {nome}\n"
            f"   📊 Parcelas: {diarias}/20"
        )
        if atraso > 0:
            linha += f" | ⚠️ {atraso}d atraso"
        if saldo > 0:
            linha += f" | ⏳ saldo R$ {saldo:.2f}"
        linha += (
            f"\n   📞 WhatsApp: {whatsapp or '-'}"
            f"\n   🪪 CPF: {cpf or '-'}"
            f"\n   💰 Diária: R$ {valor_diaria:.2f}"
            f"\n   📅 Início: {data_inicio or '-'}"
        )
        if endereco:
            linha += f"\n   📍 Endereço: {endereco}"
        if email:
            linha += f"\n   📧 Email: {email}"
        if chave_pix:
            linha += f"\n   🔑 PIX: {chave_pix}"
        if limite:
            linha += f"\n   💳 Limite: R$ {limite:.2f}"

        linhas.append(linha)

    total = len(clientes)
    corpo = "\n\n".join(linhas)

    msg = (
        f"📋 *BACKUP DIÁRIO — {hoje}*\n"
        f"{'─'*30}\n\n"
        f"{corpo}\n\n"
        f"{'─'*30}\n"
        f"👥 Total ativos: {total}\n"
        f"🟢🟡 Em dia: {em_dia}\n"
        f"🔴 Em atraso: {em_atraso}\n"
        f"⭐ Aguardando renovação: {aguardando}\n\n"
        f"🟢 em dia  🟡 saldo parcial  🔴 atrasado\n"
        f"#BACKUP_MEGACREDITO_{date.today().isoformat()}"
    )
    return msg


def job_backup_2350():
    """Backup completo às 23h50 — owner e funcionária."""
    print(f"[BOT] {datetime.now()} — Enviando backup 23h50")
    clientes = get_clientes_ativos()
    if not clientes:
        return
    msg = gerar_backup_completo(clientes)
    enviar_texto(OWNER_NUMBER, msg)
    if FUNC_NUMBER:
        enviar_texto(FUNC_NUMBER, msg)

# ── Webhook ──────────────────────────────────────────────────────

def processar_mensagem(data: dict):
    """Processa a mensagem em background — libera o webhook imediatamente."""
    msg_data   = data.get('data', {})
    key        = msg_data.get('key', {})
    remoteJid  = key.get('remoteJid', '')
    numero     = remoteJid.replace('@s.whatsapp.net', '')
    message    = msg_data.get('message', {})
    message_id = key.get('id', '')

    print(f"[BOT] De: {numero} | Chaves: {list(message.keys())}")

    tem_imagem = 'imageMessage' in message
    tem_pdf    = ('documentMessage' in message and
                  'pdf' in (message.get('documentMessage', {}).get('mimetype', '')))

    numero_limpo = re.sub(r'\D', '', numero)
    owner_limpo  = re.sub(r'\D', '', OWNER_NUMBER)
    is_owner     = numero_limpo.endswith(owner_limpo[-8:])

    # ── Owner enviou extrato para antifraude ─────────────────────
    if tem_pdf and is_owner:
        enviar_texto(OWNER_NUMBER, "📊 Extrato recebido! Processando verificação antifraude... aguarde.")
        midia = baixar_midia(message_id)
        if not midia:
            enviar_texto(OWNER_NUMBER, "❌ Não consegui baixar o extrato. Tente novamente.")
            return
        transacoes = extrair_transacoes_extrato(midia)
        print(f"[BOT] Transações extraídas: {len(transacoes)}")
        verificar_fraudes(transacoes)
        return

    # ── Cliente enviou comprovante ───────────────────────────────
    if tem_imagem or tem_pdf:
        mime  = "image/jpeg" if tem_imagem else "application/pdf"
        midia = baixar_midia(message_id)
        if not midia:
            enviar_texto(numero, "❌ Não consegui baixar o arquivo. Tente reenviar.")
            return

        # ── 1. Buscar cliente pelo número (8 últimos dígitos) ─────
        cliente = buscar_cliente_por_numero(numero)
        print(f"[BOT] buscar_cliente numero={numero} resultado={cliente}")
        if not cliente:
            enviar_texto(numero,
                "⚠️ Comprovante recebido, mas seu número não está cadastrado.\n"
                "Fale com o atendente para regularizar. 📞"
            )
            enviar_alerta_admins(
                f"⚠️ *Comprovante de número não cadastrado*\n"
                f"Número: +{numero}\n"
                f"Hora: {datetime.now().strftime('%H:%M')}"
            )
            return

        # ── 2. Extração via GPT-4o ────────────────────────────────
        dados = extrair_dados_comprovante(midia, mime)
        if not dados or not dados.get('valor'):
            # GPT não conseguiu ler — avisa owner e pede reenvio
            enviar_texto(numero,
                "⚠️ Não consegui ler o comprovante automaticamente.\n"
                "O atendente foi avisado e vai confirmar em breve! 👍"
            )
            enviar_alerta_admins(
                f"📎 *Comprovante para revisão manual*\n"
                f"Cliente: *{cliente['nome']}*\n"
                f"Número: +{numero}\n"
                f"Hora: {datetime.now().strftime('%H:%M')}\n"
                f"⚠️ Leitura automática falhou — confirme e registre manualmente."
            )
            return

        valor          = float(dados['valor'])
        hora           = dados.get('hora')
        nome_remetente = dados.get('nome_remetente') or ''
        codigo_tx      = dados.get('codigo_tx') or ''
        hash_arquivo   = hashlib.sha256(midia).hexdigest()

        # ── 3. Verificar duplicata pelo codigo_tx (ID único do PIX) ─
        if codigo_tx:
            duplicado, motivo_dup = verificar_duplicado_api('', codigo_tx)
            if duplicado:
                enviar_texto(numero,
                    "❌ Este comprovante já foi registrado anteriormente.\n"
                    "Se achar que é um erro, fale com o atendente."
                )
                enviar_alerta_admins(
                    f"🚨 *Comprovante DUPLICADO bloqueado!*\n"
                    f"Cliente: *{cliente['nome']}*\n"
                    f"Número: +{numero}\n"
                    f"Valor: R$ {valor:.2f}\n"
                    f"TX ID: {codigo_tx}"
                )
                return

        # ── 4. Validar primeiro nome (aviso ao owner, não bloqueia) ─
        if nome_remetente:
            primeiro_cadastro  = cliente['nome'].split()[0].lower()
            primeiro_remetente = nome_remetente.split()[0].lower()
            if primeiro_remetente != primeiro_cadastro:
                enviar_alerta_admins(
                    f"⚠️ *Nome diferente no comprovante — verifique*\n"
                    f"Cliente cadastrado: *{cliente['nome']}*\n"
                    f"Nome no comprovante: *{nome_remetente}*\n"
                    f"Número: +{numero} | Valor: R$ {valor:.2f}\n"
                    f"Pagamento registrado normalmente — confirme se está certo."
                )

        # ── 5. Registrar pagamento ────────────────────────────────
        obs = f"TX: {codigo_tx} | Remetente: {nome_remetente or 'n/i'} | Hash: {hash_arquivo[:12]}..."
        resultado = registrar_pagamento(cliente['id'], valor, obs, hash_arquivo, codigo_tx)
        nome = cliente['nome'].split()[0]

        if resultado:
            pag_id        = resultado.get('pag_id')
            diarias_pagas = resultado.get('diarias_pagas', cliente['diarias_pagas'])
            diarias_novas = resultado.get('diarias_novas', 0)
            restantes     = 20 - diarias_pagas

            # Salva para cruzamento com extrato
            hoje_iso = date.today().isoformat()
            if hoje_iso not in comprovantes_dia:
                comprovantes_dia[hoje_iso] = []
            comprovantes_dia[hoje_iso].append({
                'cliente_id': cliente['id'],
                'nome':       cliente['nome'],
                'valor':      valor,
                'hora':       hora,
                'pag_id':     pag_id,
            })

            tipo_cobranca = cliente.get('tipo_cobranca', 'diaria')

            if tipo_cobranca == 'mensalidade':
                parcela_paga = resultado.get('parcela_paga', False)
                pendente     = resultado.get('pendente', 0)
                if parcela_paga:
                    msg_parcelas = "📆 *Mensalidade quitada!* ✅"
                else:
                    msg_parcelas = f"⏳ Pagamento parcial. Pendente: R$ {pendente:.2f}"
            else:
                diarias_pagas = resultado.get('diarias_pagas', cliente.get('diarias_pagas', 0))
                diarias_novas = resultado.get('diarias_novas', 0)
                total_diarias = cliente.get('total_diarias', 20)
                restantes     = total_diarias - diarias_pagas
                if diarias_pagas >= total_diarias:
                    msg_parcelas = f"🎉 *Parabéns! Você completou todas as {total_diarias} diárias!*\nAguarde a renovação do contrato."
                elif diarias_novas == 0:
                    msg_parcelas = "⏳ Pagamento parcial registrado. Continue pagando para completar a próxima diária."
                else:
                    msg_parcelas = (
                        f"📊 *{diarias_pagas}/{total_diarias} diárias pagas*\n"
                        f"✅ +{diarias_novas} diária(s) neste pagamento\n"
                        f"📅 Faltam {restantes} diária(s) para concluir"
                    )

            dias_restantes = resultado.get('dias_em_atraso', 0)
            aviso_atraso   = ""
            if dias_restantes > 0:
                aviso_atraso = "\n\n" + gerar_aviso_dias_atraso(dias_restantes) + \
                               f" ainda em aberto\n💸 Valor pendente: R$ {resultado.get('valor_em_atraso', 0):.2f}"

            # Confirmação só para o cliente e para o owner
            enviar_texto(numero,
                f"✅ *Pagamento confirmado, {nome}!*\n\n"
                f"💰 Valor: R$ {valor:.2f}\n\n"
                f"{msg_parcelas}"
                f"{aviso_atraso}\n\n"
                f"Obrigado! 🙏"
            )
            # Notificação de pagamento removida — resumo diário às 23h já cobre isso
        else:
            enviar_texto(numero,
                f"⚠️ Comprovante recebido (R$ {valor:.2f}), mas ocorreu um erro ao registrar. "
                f"Fale com o atendente."
            )
            # Erro técnico — só owner, não funcionária
            enviar_texto(OWNER_NUMBER,
                f"🚨 *Erro ao registrar pagamento!*\n"
                f"Cliente: {cliente['nome']}\n"
                f"Valor: R$ {valor:.2f}\n"
                f"Número: +{numero}\n"
                f"TX: {codigo_tx or 'n/i'}\n"
                f"Verificar manualmente."
            )

    elif 'conversation' in message or 'extendedTextMessage' in message:
        texto_raw = (message.get('conversation') or
                     message.get('extendedTextMessage', {}).get('text', '')).strip()
        texto     = texto_raw.lower()

        # ── Comandos ocultos do owner ─────────────────────────────
        if is_owner:

            # #resumodia — resumo completo a qualquer hora
            if '#resumodia' in texto:
                clientes = get_clientes_ativos()
                if not clientes:
                    enviar_texto(OWNER_NUMBER, "⚠️ Nenhum cliente ativo no momento.")
                    return
                hoje = date.today().strftime('%d/%m/%Y')
                linhas = []
                for c in clientes:
                    diarias = c['diarias_pagas']
                    atraso  = c['dias_em_atraso']
                    if c['status'] == 'aguardando':
                        icone = "⭐"
                    elif atraso > 0:
                        icone = "🔴"
                    elif c['saldo_pendente'] > 0:
                        icone = "🟡"
                    else:
                        icone = "🟢"
                    info = f"{diarias}/20"
                    if atraso > 0:
                        info += f" | ⚠️ {atraso}d atraso"
                    linhas.append(f"{icone} {c['nome']} — {info}")
                corpo = "\n".join(linhas)
                msg = (
                    f"📊 *RESUMO DO DIA — {hoje}*\n"
                    f"{'─'*30}\n\n"
                    f"{corpo}\n\n"
                    f"{'─'*30}\n"
                    f"👥 Total ativos: {len(clientes)}"
                )
                enviar_texto(OWNER_NUMBER, msg)
                return

            # #atualizeiosite — recebe backup (texto) e sincroniza o banco
            if '#atualizeiosite' in texto:
                # Extrai os dados do backup da própria mensagem
                # Formato: linhas com "Nome\n   📞 WhatsApp: ... 📊 Parcelas: X/20 ..."
                clientes_parse = []
                # Parse do formato gerado pelo gerar_backup_completo
                blocos = re.split(r'\n(?=[🟢🟡🔴⭐])', texto_raw)
                for bloco in blocos:
                    try:
                        nome_m       = re.search(r'[🟢🟡🔴⭐]\s+(.+)', bloco)
                        parcelas_m   = re.search(r'Parcelas:\s*(\d+)/20', bloco)
                        whatsapp_m   = re.search(r'WhatsApp:\s*(\S+)', bloco)
                        cpf_m        = re.search(r'CPF:\s*(\S+)', bloco)
                        diaria_m     = re.search(r'Diária:\s*R\$\s*([\d,.]+)', bloco)
                        inicio_m     = re.search(r'Início:\s*(\S+)', bloco)
                        endereco_m   = re.search(r'Endereço:\s*(.+)', bloco)
                        email_m      = re.search(r'Email:\s*(\S+)', bloco)
                        pix_m        = re.search(r'PIX:\s*(\S+)', bloco)
                        limite_m     = re.search(r'Limite:\s*R\$\s*([\d,.]+)', bloco)
                        saldo_m      = re.search(r'saldo R\$\s*([\d,.]+)', bloco)

                        if not nome_m or not parcelas_m:
                            continue

                        nome     = nome_m.group(1).strip()
                        diarias  = int(parcelas_m.group(1))
                        whatsapp = whatsapp_m.group(1).strip() if whatsapp_m else ''
                        if whatsapp == '-':
                            whatsapp = ''
                        cpf      = cpf_m.group(1).strip() if cpf_m else ''
                        if cpf == '-':
                            cpf = ''
                        diaria_val = float(diaria_m.group(1).replace(',', '.')) if diaria_m else 0
                        inicio   = inicio_m.group(1).strip() if inicio_m else ''
                        if inicio == '-':
                            inicio = ''
                        # Converte data_inicio de dd/mm/aaaa para aaaa-mm-dd se necessário
                        if inicio and '/' in inicio:
                            partes = inicio.split('/')
                            if len(partes) == 3:
                                inicio = f"{partes[2]}-{partes[1]}-{partes[0]}"
                        endereco = endereco_m.group(1).strip() if endereco_m else ''
                        email    = email_m.group(1).strip() if email_m else ''
                        pix      = pix_m.group(1).strip() if pix_m else ''
                        limite   = float(limite_m.group(1).replace(',', '.')) if limite_m else 0
                        saldo    = float(saldo_m.group(1).replace(',', '.')) if saldo_m else 0

                        clientes_parse.append({
                            'nome':           nome,
                            'whatsapp':       whatsapp,
                            'cpf':            cpf,
                            'valor_diaria':   diaria_val,
                            'data_inicio':    inicio,
                            'diarias_pagas':  diarias,
                            'saldo_pendente': saldo,
                            'endereco':       endereco,
                            'email':          email,
                            'chave_pix':      pix,
                            'limite':         limite,
                        })
                    except Exception as e:
                        print(f"[BOT] Erro ao parsear bloco de backup: {e}")
                        continue

                if not clientes_parse:
                    enviar_texto(OWNER_NUMBER,
                        "❌ Não consegui interpretar o backup enviado.\n"
                        "Certifique-se de enviar exatamente a mensagem de backup gerada pelo bot."
                    )
                    return

                enviar_texto(OWNER_NUMBER,
                    f"⏳ Processando backup de *{len(clientes_parse)}* clientes..."
                )
                resultado = upsert_clientes(clientes_parse)
                if resultado.get('ok'):
                    erros_txt = ''
                    if resultado.get('erros'):
                        erros_txt = '\n⚠️ Erros:\n' + '\n'.join(f'  • {e}' for e in resultado['erros'])
                    enviar_texto(OWNER_NUMBER,
                        f"✅ *Backup aplicado com sucesso!*\n\n"
                        f"✅ Cadastrados: {resultado.get('cadastrados', 0)}\n"
                        f"🔄 Atualizados: {resultado.get('atualizados', 0)}\n"
                        f"⏭️ Ignorados (igual/maior): {resultado.get('ignorados', 0)}"
                        f"{erros_txt}"
                    )
                else:
                    enviar_texto(OWNER_NUMBER, "❌ Erro ao aplicar backup. Verifique os logs.")
                return

        # ── Mensagens normais de clientes ─────────────────────────
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
            enviar_alerta_admins(
                f"🔔 *Cliente quer falar com atendente!*\n"
                f"Número: +{numero}\n"
                f"Hora: {datetime.now().strftime('%H:%M')}"
            )


@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    print(f"[BOT] PAYLOAD: {json.dumps(data, ensure_ascii=False)[:500]}")

    evento = data.get('event', '')
    if evento not in ('messages.upsert', 'message.received'):
        return jsonify(ok=True)

    key = data.get('data', {}).get('key', {})
    if key.get('fromMe'):
        return jsonify(ok=True)

    # Responde imediatamente e processa em background
    threading.Thread(target=processar_mensagem, args=(data,), daemon=True).start()
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

@app.route('/disparar/backup', methods=['POST'])
def disparar_backup():
    if request.headers.get('X-Secret') != BOT_SECRET:
        return jsonify(erro="não autorizado"), 403
    job_backup_2350()
    return jsonify(ok=True, msg="Backup enviado")

@app.route('/health')
def health():
    return jsonify(status="ok", hora=datetime.now().isoformat())

# ── Inicialização ────────────────────────────────────────────────

try:
    scheduler = BackgroundScheduler(timezone="America/Manaus")
    scheduler.add_job(job_cobranca_18h, 'cron', hour=18, minute=0)
    scheduler.add_job(job_resumo_23h,   'cron', hour=23, minute=0)
    scheduler.add_job(job_backup_2350,  'cron', hour=23, minute=50)
    scheduler.start()
    print("[BOT] Scheduler iniciado com sucesso")
except Exception as e:
    print(f"[BOT] ERRO ao iniciar scheduler: {e}")

print(f"[BOT] App pronto — EVOLUTION_URL={EVOLUTION_URL} MEGACREDITO_URL={MEGACREDITO_URL}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"[BOT] Iniciando na porta {port}")
    app.run(host='0.0.0.0', port=port)
