import hashlib
import time
import requests
import logging
import os
import json as json_module
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, Request, BackgroundTasks

# ============================================================
# CONFIGURACAO DE LOGS - MOSTRA O QUE ESTA ACONTECENDO
# Voce vera essas mensagens na aba "Logs" do Render
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI()

# Guarda o ultimo webhook recebido e o ultimo processamento (em memoria)
# so pra diagnostico via /ultimo-webhook — nao usar isso pra nada que
# precise persistir de verdade.
ultimo_webhook_recebido = {}
ultimo_processamento = {}

# ============================================================
# LEITURA DAS CONFIGURACOES (via variaveis de ambiente do Render)
# NUNCA coloque as chaves direto aqui no codigo!
# Configure-as na aba "Environment" do seu servico no Render.
# ============================================================
PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN", "")
PIPEDRIVE_DOMAIN    = os.environ.get("PIPEDRIVE_DOMAIN", "")  # so a parte antes de .pipedrive.com

# ID da etapa "Qualificado" no funil do Pipedrive.
# Descubra acessando a rota /listar-etapas depois do deploy.
QUALIFICADO_STAGE_ID = os.environ.get("QUALIFICADO_STAGE_ID", "")

META_PIXEL_ID       = os.environ.get("META_PIXEL_ID", "")
META_ACCESS_TOKEN   = os.environ.get("META_ACCESS_TOKEN", "")
META_API_VERSION    = "v21.0"

# Token permanente da Pagina, usado so pra buscar dados do anuncio
# (ad/adset/campaign) a partir do Lead ID — nao e o mesmo token da
# API de Conversoes.
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")

# Key do campo "Facebook Lead ID" na Pessoa (criado via /criar-campo-facebook-lead-id)
CAMPO_PESSOA_LEAD_ID = "68a72231ac7f6da81c7793592cb465b1ff85d033"

# Keys dos campos customizados do Negocio que guardam dados do anuncio
CAMPO_DEAL_CANAL    = "c468b47fdde85ddbffb48c820157cd18dbec55c6"   # nome da campanha
CAMPO_DEAL_CONJUNTO = "a48dbca6afcad8e8c81a96643d1b45c0296ec273"   # nome do conjunto (adset)
CAMPO_DEAL_ANUNCIO  = "b9efca488ccd4ba3c07eb3f0b78d07d0aa1316b5"   # nome do anuncio (ad)

NOME_EVENTO_QUALIFICADO = "MQLqualificado"
NOME_EVENTO_LEAD_INICIAL = "Lead"
CUSTOM_DATA_LEAD_INICIAL = {"event_source": "crm", "lead_event_source": "Pipedrive"}


# ============================================================
# FUNCOES DE SEGURANCA E CRIPTOGRAFIA
# ============================================================

def hash_telefone(telefone: str) -> str:
    """
    Limpa o numero, adiciona codigo do Brasil (55) se necessario
    e transforma em SHA-256 (exigencia da Meta por privacidade).
    """
    if not telefone:
        return ""
    apenas_numeros = "".join(filter(str.isdigit, str(telefone)))
    if not apenas_numeros:
        return ""
    if len(apenas_numeros) <= 11:
        apenas_numeros = "55" + apenas_numeros
    resultado = hashlib.sha256(apenas_numeros.encode("utf-8")).hexdigest()
    logger.info(f"Telefone processado: {apenas_numeros[:4]}*** -> hash gerado")
    return resultado


def hash_email(email: str) -> str:
    """Coloca em minusculo, remove espacos e transforma em SHA-256."""
    if not email or "@" not in email:
        return ""
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()


# ============================================================
# LEITURA DA ETAPA (STAGE) NO PAYLOAD DO WEBHOOK
# ============================================================

def extrair_stage_id(dados_negocio: dict) -> Optional[str]:
    if not dados_negocio:
        return None
    stage_id = dados_negocio.get("stage_id")
    if isinstance(stage_id, dict):
        stage_id = stage_id.get("value")
    return str(stage_id) if stage_id is not None else None


def extrair_person_id(dados_negocio: dict) -> Optional[int]:
    person_id = dados_negocio.get("person_id")
    if isinstance(person_id, dict):
        person_id = person_id.get("value")
    try:
        return int(person_id) if person_id else None
    except (TypeError, ValueError):
        return None


LIMITE_NEGOCIO_NOVO_SEGUNDOS = 300  # 5 minutos


def eh_negocio_recente(add_time: Optional[str]) -> bool:
    """Considera 'negocio novo' quando foi criado ha poucos minutos —
    mais confiavel do que o campo 'action' do webhook, que o Pipedrive
    manda como 'change' tanto pra criacao quanto pra atualizacao."""
    if not add_time:
        return False
    try:
        criado_em = datetime.fromisoformat(add_time.replace("Z", "+00:00"))
        agora = datetime.now(timezone.utc)
        return (agora - criado_em).total_seconds() <= LIMITE_NEGOCIO_NOVO_SEGUNDOS
    except (ValueError, TypeError):
        return False


# ============================================================
# BUSCA DE DADOS DA PESSOA NO PIPEDRIVE
# O webhook do negocio nao traz e-mail/telefone — so o ID da
# pessoa vinculada. Por isso e preciso uma segunda chamada.
# ============================================================

def buscar_pessoa_pipedrive(person_id: int) -> dict:
    url = f"https://{PIPEDRIVE_DOMAIN}.pipedrive.com/api/v1/persons/{person_id}"
    try:
        resp = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Erro ao buscar pessoa {person_id}: {resp.status_code} - {resp.text[:300]}")
            return {}
        return resp.json().get("data") or {}
    except Exception as e:
        logger.error(f"Excecao ao buscar pessoa {person_id}: {e}")
        return {}


# ============================================================
# DADOS DO ANUNCIO NO FACEBOOK (a partir do Lead ID)
# ============================================================

def buscar_dados_anuncio_facebook(lead_id: str) -> Optional[dict]:
    """Usa o Lead ID (leadgen_id) pra buscar de qual anuncio/conjunto/
    campanha esse lead especifico veio, direto na API do Facebook."""
    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        logger.warning("FACEBOOK_PAGE_ACCESS_TOKEN nao configurado — pulando busca de dados do anuncio")
        return None

    url = f"https://graph.facebook.com/{META_API_VERSION}/{lead_id}"
    params = {
        "fields": "ad_name,adset_name,campaign_name",
        "access_token": FACEBOOK_PAGE_ACCESS_TOKEN
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Erro ao buscar dados do anuncio pro lead {lead_id}: {resp.status_code} - {resp.text[:300]}")
            return None
        return resp.json()
    except Exception as e:
        logger.error(f"Excecao ao buscar dados do anuncio: {e}")
        return None


def atualizar_campos_anuncio_negocio(deal_id: str, dados_anuncio: dict) -> bool:
    """Preenche ANUNCIO/CANAL/CONJUNTO no negocio com os dados vindos do Facebook."""
    campos = {}
    if dados_anuncio.get("campaign_name"):
        campos[CAMPO_DEAL_CANAL] = dados_anuncio["campaign_name"]
    if dados_anuncio.get("adset_name"):
        campos[CAMPO_DEAL_CONJUNTO] = dados_anuncio["adset_name"]
    if dados_anuncio.get("ad_name"):
        campos[CAMPO_DEAL_ANUNCIO] = dados_anuncio["ad_name"]

    if not campos:
        return False

    url = f"https://{PIPEDRIVE_DOMAIN}.pipedrive.com/api/v1/deals/{deal_id}"
    try:
        resp = requests.put(url, params={"api_token": PIPEDRIVE_API_TOKEN}, json=campos, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Erro ao atualizar campos de anuncio do negocio {deal_id}: {resp.status_code} - {resp.text[:300]}")
            return False
        logger.info(f"Negocio {deal_id}: campos ANUNCIO/CANAL/CONJUNTO preenchidos")
        return True
    except Exception as e:
        logger.error(f"Excecao ao atualizar campos de anuncio: {e}")
        return False


# ============================================================
# ENVIO PARA A API DE CONVERSOES DA META
# ============================================================

def enviar_evento_meta(tel_hash: str, email_hash: str, deal_id: str, nome_evento: str,
                        event_id_prefixo: str, custom_data: Optional[dict] = None):
    if not META_PIXEL_ID or not META_ACCESS_TOKEN:
        logger.warning("Meta nao configurado — pulando envio")
        return {"erro": "Meta nao configurado"}

    user_data = {}
    if tel_hash:
        user_data["ph"] = [tel_hash]
    if email_hash:
        user_data["em"] = [email_hash]

    if not user_data:
        logger.warning(f"Negocio {deal_id}: sem telefone e sem email, evento nao enviado")
        return {"erro": "sem telefone e sem email"}

    evento = {
        "event_name": nome_evento,
        "event_time": int(time.time()),
        "action_source": "system_generated",
        "event_id": f"{event_id_prefixo}_{deal_id}",
        "user_data": user_data
    }
    if custom_data:
        evento["custom_data"] = custom_data

    payload = {"data": [evento], "access_token": META_ACCESS_TOKEN}

    try:
        url = f"https://graph.facebook.com/{META_API_VERSION}/{META_PIXEL_ID}/events"
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            logger.info(f"META CAPI OK — Evento: {nome_evento} | Negocio: {deal_id}")
        else:
            logger.error(f"META CAPI Erro {r.status_code}: {r.text}")
        return {"status_code": r.status_code, "resposta": r.text[:500]}
    except Exception as e:
        logger.error(f"META CAPI excecao: {e}")
        return {"erro": str(e)}


# ============================================================
# PROCESSAMENTO EM SEGUNDO PLANO
# ============================================================

def buscar_hashes_contato(deal_id: str, person_id: int, ultimo_processamento: dict) -> Optional[tuple]:
    """Busca a pessoa, extrai telefone/email e devolve (tel_hash, email_hash, pessoa)."""
    pessoa = buscar_pessoa_pipedrive(person_id)
    if not pessoa:
        logger.warning(f"Negocio {deal_id}: nao foi possivel obter dados da pessoa {person_id}")
        ultimo_processamento["erro"] = "nao foi possivel obter dados da pessoa"
        return None

    telefone = ""
    email = ""

    telefones = pessoa.get("phone") or []
    if telefones:
        telefone = telefones[0].get("value", "")

    emails = pessoa.get("email") or []
    if emails:
        email = emails[0].get("value", "")

    ultimo_processamento["tinha_telefone"] = bool(telefone)
    ultimo_processamento["tinha_email"] = bool(email)

    tel_hash = hash_telefone(telefone)
    email_hash = hash_email(email)

    if not tel_hash and not email_hash:
        logger.warning(f"Negocio {deal_id}: pessoa sem telefone e sem email. Nenhum evento enviado.")
        ultimo_processamento["erro"] = "pessoa sem telefone e sem email"
        return None

    return tel_hash, email_hash, pessoa


def processar_negocio_qualificado(deal_id: str, person_id: int):
    logger.info(f"--- Processando negocio {deal_id} (entrou na etapa Qualificado) ---")

    global ultimo_processamento
    ultimo_processamento = {"deal_id": deal_id, "person_id": person_id, "etapa": "buscando pessoa"}

    hashes = buscar_hashes_contato(deal_id, person_id, ultimo_processamento)
    if not hashes:
        return
    tel_hash, email_hash, _pessoa = hashes

    resultado_meta = enviar_evento_meta(tel_hash, email_hash, deal_id, NOME_EVENTO_QUALIFICADO, "pipedrive_qualificado")
    ultimo_processamento["resultado_meta"] = resultado_meta
    logger.info(f"--- Negocio {deal_id} finalizado ---")


def processar_lead_inicial(deal_id: str, person_id: int):
    logger.info(f"--- Processando negocio {deal_id} (lead novo) ---")

    global ultimo_processamento
    ultimo_processamento = {"deal_id": deal_id, "person_id": person_id, "etapa": "buscando pessoa (lead inicial)"}

    hashes = buscar_hashes_contato(deal_id, person_id, ultimo_processamento)
    if not hashes:
        return
    tel_hash, email_hash, pessoa = hashes

    resultado_meta = enviar_evento_meta(
        tel_hash, email_hash, deal_id, NOME_EVENTO_LEAD_INICIAL, "pipedrive_lead",
        custom_data=CUSTOM_DATA_LEAD_INICIAL
    )
    ultimo_processamento["resultado_meta"] = resultado_meta

    # Preenche ANUNCIO/CANAL/CONJUNTO usando o Lead ID do Facebook
    # salvo na Pessoa (vindo da bridge do LeadsBridge).
    lead_id_facebook = pessoa.get(CAMPO_PESSOA_LEAD_ID)
    ultimo_processamento["tinha_lead_id_facebook"] = bool(lead_id_facebook)

    if lead_id_facebook:
        dados_anuncio = buscar_dados_anuncio_facebook(lead_id_facebook)
        if dados_anuncio:
            atualizou = atualizar_campos_anuncio_negocio(deal_id, dados_anuncio)
            ultimo_processamento["campos_anuncio_preenchidos"] = atualizou
        else:
            ultimo_processamento["campos_anuncio_preenchidos"] = False

    logger.info(f"--- Negocio {deal_id} finalizado (lead inicial) ---")


# ============================================================
# ROTAS DO SERVIDOR
# ============================================================

@app.get("/")
def rota_raiz():
    """Acesse no navegador para confirmar que o servidor esta no ar."""
    return {
        "status": "online",
        "mensagem": "Ponte Pipedrive -> Meta CAPI funcionando!",
        "rotas": {
            "verificar": "/verificar",
            "listar_campos_negocio": "/listar-campos-negocio",
            "webhook_pipedrive": "/webhook-pipedrive"
        }
    }


@app.get("/verificar")
def verificar_configuracao():
    """Mostra se cada variavel de ambiente esta configurada (sem revelar os valores)."""
    return {
        "pipedrive_api_token": "configurado" if PIPEDRIVE_API_TOKEN else "FALTANDO",
        "pipedrive_domain":    "configurado" if PIPEDRIVE_DOMAIN else "FALTANDO",
        "qualificado_stage_id": "configurado" if QUALIFICADO_STAGE_ID else "FALTANDO (veja /listar-etapas)",
        "meta_pixel_id":       "configurado" if META_PIXEL_ID else "FALTANDO",
        "meta_access_token":   "configurado" if META_ACCESS_TOKEN else "FALTANDO",
        "facebook_page_access_token": "configurado" if FACEBOOK_PAGE_ACCESS_TOKEN else "FALTANDO (opcional, so pra preencher ANUNCIO/CANAL/CONJUNTO)",
    }


@app.get("/listar-campos-negocio")
def listar_campos_negocio():
    """
    Rota de diagnostico geral: lista todos os campos customizados de
    negocio do Pipedrive (nome, key e opcoes). Nao e usada no fluxo
    principal (que agora usa etapa do funil, veja /listar-etapas),
    mas fica disponivel caso seja preciso inspecionar outro campo.
    """
    if not PIPEDRIVE_API_TOKEN or not PIPEDRIVE_DOMAIN:
        return {"erro": "PIPEDRIVE_API_TOKEN ou PIPEDRIVE_DOMAIN nao configurados no Render"}

    url = f"https://{PIPEDRIVE_DOMAIN}.pipedrive.com/api/v1/dealFields"
    try:
        resp = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=10)
    except Exception as e:
        return {"erro": f"Falha ao conectar ao Pipedrive: {e}"}

    if resp.status_code != 200:
        return {"erro": f"Pipedrive retornou status {resp.status_code}", "detalhe": resp.text[:1000]}

    campos = []
    for campo in resp.json().get("data") or []:
        item = {
            "nome": campo.get("name"),
            "key": campo.get("key"),
            "tipo": campo.get("field_type"),
        }
        opcoes = campo.get("options")
        if opcoes:
            item["opcoes"] = [{"id": o.get("id"), "label": o.get("label")} for o in opcoes]
        campos.append(item)

    return {"campos": campos}


@app.post("/criar-campo-facebook-lead-id")
def criar_campo_facebook_lead_id():
    """
    Cria (se ainda nao existir) um campo customizado "Facebook Lead ID"
    na Pessoa do Pipedrive, pra guardar o lead_id vindo do Facebook.
    Seguro rodar mais de uma vez — nao duplica se ja existir.
    """
    if not PIPEDRIVE_API_TOKEN or not PIPEDRIVE_DOMAIN:
        return {"erro": "PIPEDRIVE_API_TOKEN ou PIPEDRIVE_DOMAIN nao configurados no Render"}

    url = f"https://{PIPEDRIVE_DOMAIN}.pipedrive.com/api/v1/personFields"

    try:
        resp_existentes = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=10)
        for campo in resp_existentes.json().get("data") or []:
            if campo.get("name") == "Facebook Lead ID":
                return {"mensagem": "Campo ja existia", "key": campo.get("key")}
    except Exception as e:
        return {"erro": f"Falha ao consultar campos existentes: {e}"}

    try:
        resp_criacao = requests.post(
            url,
            params={"api_token": PIPEDRIVE_API_TOKEN},
            json={"name": "Facebook Lead ID", "field_type": "varchar"},
            timeout=10
        )
    except Exception as e:
        return {"erro": f"Falha ao criar campo: {e}"}

    if resp_criacao.status_code not in (200, 201):
        return {"erro": f"Pipedrive retornou status {resp_criacao.status_code}", "detalhe": resp_criacao.text[:1000]}

    novo_campo = resp_criacao.json().get("data") or {}
    return {"mensagem": "Campo criado com sucesso", "key": novo_campo.get("key")}


@app.get("/listar-etapas")
def listar_etapas():
    """
    Rota auxiliar para descobrir o ID da etapa "Qualificado" no funil.
    Acesse essa rota, procure a etapa chamada "Qualificado" e copie o
    "id" pra variavel QUALIFICADO_STAGE_ID no Render.
    """
    if not PIPEDRIVE_API_TOKEN or not PIPEDRIVE_DOMAIN:
        return {"erro": "PIPEDRIVE_API_TOKEN ou PIPEDRIVE_DOMAIN nao configurados no Render"}

    url = f"https://{PIPEDRIVE_DOMAIN}.pipedrive.com/api/v1/stages"
    try:
        resp = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=10)
    except Exception as e:
        return {"erro": f"Falha ao conectar ao Pipedrive: {e}"}

    if resp.status_code != 200:
        return {"erro": f"Pipedrive retornou status {resp.status_code}", "detalhe": resp.text[:1000]}

    etapas = []
    for etapa in resp.json().get("data") or []:
        etapas.append({
            "id": etapa.get("id"),
            "nome": etapa.get("name"),
            "pipeline_id": etapa.get("pipeline_id"),
        })

    return {"etapas": etapas}


@app.get("/ultimo-webhook")
def ultimo_webhook():
    """
    Rota de diagnostico: mostra o ultimo webhook que o servidor recebeu
    do Pipedrive (fica so em memoria, some se o servidor reiniciar).
    Util pra conferir se o webhook chegou e em que formato.
    """
    if not ultimo_webhook_recebido:
        return {"mensagem": "Nenhum webhook recebido ainda"}
    return {**ultimo_webhook_recebido, "processamento": ultimo_processamento or "nao processado (negocio nao entrou na etapa Qualificado)"}


@app.post("/webhook-pipedrive")
async def receber_webhook_pipedrive(request: Request, background_tasks: BackgroundTasks):
    """
    Recebe o webhook do Pipedrive quando um negocio e atualizado.

    Configurar no Pipedrive: Ferramentas e integracoes > Webhooks
      - Evento: "Updated" / "deal"
      - URL: https://SEU-LINK.onrender.com/webhook-pipedrive
    """
    corpo = await request.json()

    # Log do payload bruto — essencial na primeira configuracao pra
    # confirmar o formato exato que o Pipedrive esta enviando.
    logger.info(f"========== WEBHOOK PIPEDRIVE RECEBIDO ==========")
    logger.info(f"Payload: {json_module.dumps(corpo)[:2000]}")

    global ultimo_webhook_recebido, ultimo_processamento
    ultimo_webhook_recebido = {"recebido_em": time.strftime("%Y-%m-%d %H:%M:%S"), "payload": corpo}
    ultimo_processamento = {}

    # O Pipedrive pode chamar o objeto atual de "current" ou "data"
    # dependendo da versao do webhook — tentamos os dois.
    atual = corpo.get("current") or corpo.get("data") or {}
    anterior = corpo.get("previous") or {}

    deal_id = atual.get("id", "desconhecido")

    # O Pipedrive manda "action: change" tanto pra criacao quanto pra
    # atualizacao — nao da pra confiar nesse campo. Em vez disso,
    # tratamos como negocio novo quando "add_time" e muito recente
    # (poucos minutos atras).
    negocio_e_novo = eh_negocio_recente(atual.get("add_time"))

    logger.info(f"Negocio {deal_id} — Negocio novo? {negocio_e_novo}")

    # Negocio recem-criado: manda o evento "Lead" inicial imediatamente,
    # sem depender de etapa (a Meta pede esse envio pra melhorar a
    # cobertura de leads na integracao do CRM).
    if negocio_e_novo:
        ultimo_processamento = {"deal_id": deal_id, "add_time": atual.get("add_time")}
        person_id = extrair_person_id(atual)
        if not person_id:
            logger.warning(f"Negocio {deal_id}: sem pessoa vinculada, lead inicial nao enviado")
            ultimo_processamento["decisao"] = "ignorado: sem pessoa vinculada"
            return {"status": "ignorado", "motivo": "negocio sem pessoa vinculada"}

        ultimo_processamento["decisao"] = "processando lead inicial"
        background_tasks.add_task(processar_lead_inicial, str(deal_id), person_id)
        return {"status": "recebido", "deal_id": deal_id, "tipo": "lead_inicial"}

    stage_atual = extrair_stage_id(atual)
    stage_anterior = extrair_stage_id(anterior)

    logger.info(f"Negocio {deal_id} — Etapa atual: {stage_atual} | Etapa anterior: {stage_anterior}")
    ultimo_processamento = {"deal_id": deal_id, "stage_atual": stage_atual, "stage_anterior": stage_anterior}

    if not QUALIFICADO_STAGE_ID:
        ultimo_processamento["decisao"] = "ignorado: QUALIFICADO_STAGE_ID nao configurado"
        return {"status": "ignorado", "motivo": "QUALIFICADO_STAGE_ID nao configurado no Render"}

    if stage_atual != QUALIFICADO_STAGE_ID:
        ultimo_processamento["decisao"] = "ignorado: negocio nao esta na etapa Qualificado"
        return {"status": "ignorado", "motivo": "negocio nao esta na etapa Qualificado", "stage_atual": stage_atual}

    if stage_anterior == QUALIFICADO_STAGE_ID:
        ultimo_processamento["decisao"] = "ignorado: ja estava na etapa Qualificado antes"
        return {"status": "ignorado", "motivo": "ja estava na etapa Qualificado antes (evita duplicidade)"}

    person_id = extrair_person_id(atual)
    if not person_id:
        logger.warning(f"Negocio {deal_id}: sem pessoa vinculada, evento nao enviado")
        ultimo_processamento["decisao"] = "ignorado: sem pessoa vinculada"
        return {"status": "ignorado", "motivo": "negocio sem pessoa vinculada"}

    ultimo_processamento["decisao"] = "processando"

    # Processa em background para responder ao Pipedrive rapido
    background_tasks.add_task(processar_negocio_qualificado, str(deal_id), person_id)

    return {"status": "recebido", "deal_id": deal_id}
