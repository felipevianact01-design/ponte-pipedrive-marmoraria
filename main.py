import hashlib
import time
import requests
import logging
import os
import json as json_module
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

# ============================================================
# LEITURA DAS CONFIGURACOES (via variaveis de ambiente do Render)
# NUNCA coloque as chaves direto aqui no codigo!
# Configure-as na aba "Environment" do seu servico no Render.
# ============================================================
PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN", "")
PIPEDRIVE_DOMAIN    = os.environ.get("PIPEDRIVE_DOMAIN", "")  # so a parte antes de .pipedrive.com

# Chave do campo customizado MQL e ID da opcao "Qualificado".
# Descubra os dois valores acessando a rota /listar-campos-negocio depois do deploy.
MQL_FIELD_KEY       = os.environ.get("MQL_FIELD_KEY", "")
MQL_QUALIFICADO_ID  = os.environ.get("MQL_QUALIFICADO_ID", "77")

META_PIXEL_ID       = os.environ.get("META_PIXEL_ID", "")
META_ACCESS_TOKEN   = os.environ.get("META_ACCESS_TOKEN", "")
META_API_VERSION    = "v21.0"

NOME_EVENTO_META    = "MQLqualificado"


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
# LEITURA DO CAMPO MQL NO PAYLOAD DO WEBHOOK
# O Pipedrive pode enviar o valor do campo customizado de formas
# ligeiramente diferentes dependendo da versao do webhook — por
# isso a funcao abaixo tenta alguns formatos comuns.
# ============================================================

def extrair_valor_mql(dados_negocio: dict) -> Optional[str]:
    if not MQL_FIELD_KEY or not dados_negocio:
        return None

    valor = dados_negocio.get(MQL_FIELD_KEY)

    if valor is None:
        campos_customizados = dados_negocio.get("custom_fields") or {}
        valor = campos_customizados.get(MQL_FIELD_KEY)

    if isinstance(valor, dict):
        valor = valor.get("value", valor.get("id"))

    if isinstance(valor, list) and valor:
        primeiro = valor[0]
        valor = primeiro.get("value", primeiro.get("id")) if isinstance(primeiro, dict) else primeiro

    return str(valor) if valor is not None else None


def extrair_person_id(dados_negocio: dict) -> Optional[int]:
    person_id = dados_negocio.get("person_id")
    if isinstance(person_id, dict):
        person_id = person_id.get("value")
    try:
        return int(person_id) if person_id else None
    except (TypeError, ValueError):
        return None


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
# ENVIO PARA A API DE CONVERSOES DA META
# ============================================================

def enviar_evento_meta(tel_hash: str, email_hash: str, deal_id: str):
    if not META_PIXEL_ID or not META_ACCESS_TOKEN:
        logger.warning("Meta nao configurado — pulando envio")
        return

    user_data = {}
    if tel_hash:
        user_data["ph"] = [tel_hash]
    if email_hash:
        user_data["em"] = [email_hash]

    if not user_data:
        logger.warning(f"Negocio {deal_id}: sem telefone e sem email, evento nao enviado")
        return

    payload = {
        "data": [{
            "event_name": NOME_EVENTO_META,
            "event_time": int(time.time()),
            "action_source": "system_generated",
            "event_id": f"pipedrive_mql_{deal_id}",
            "user_data": user_data
        }],
        "access_token": META_ACCESS_TOKEN
    }

    try:
        url = f"https://graph.facebook.com/{META_API_VERSION}/{META_PIXEL_ID}/events"
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            logger.info(f"META CAPI OK — Evento: {NOME_EVENTO_META} | Negocio: {deal_id}")
        else:
            logger.error(f"META CAPI Erro {r.status_code}: {r.text}")
    except Exception as e:
        logger.error(f"META CAPI excecao: {e}")


# ============================================================
# PROCESSAMENTO EM SEGUNDO PLANO
# ============================================================

def processar_negocio_qualificado(deal_id: str, person_id: int):
    logger.info(f"--- Processando negocio {deal_id} (MQL qualificado) ---")

    pessoa = buscar_pessoa_pipedrive(person_id)
    if not pessoa:
        logger.warning(f"Negocio {deal_id}: nao foi possivel obter dados da pessoa {person_id}")
        return

    telefone = ""
    email = ""

    telefones = pessoa.get("phone") or []
    if telefones:
        telefone = telefones[0].get("value", "")

    emails = pessoa.get("email") or []
    if emails:
        email = emails[0].get("value", "")

    tel_hash = hash_telefone(telefone)
    email_hash = hash_email(email)

    if not tel_hash and not email_hash:
        logger.warning(f"Negocio {deal_id}: pessoa sem telefone e sem email. Nenhum evento enviado.")
        return

    enviar_evento_meta(tel_hash, email_hash, deal_id)
    logger.info(f"--- Negocio {deal_id} finalizado ---")


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
        "mql_field_key":       "configurado" if MQL_FIELD_KEY else "FALTANDO (veja /listar-campos-negocio)",
        "mql_qualificado_id":  MQL_QUALIFICADO_ID,
        "meta_pixel_id":       "configurado" if META_PIXEL_ID else "FALTANDO",
        "meta_access_token":   "configurado" if META_ACCESS_TOKEN else "FALTANDO",
    }


@app.get("/listar-campos-negocio")
def listar_campos_negocio():
    """
    Rota auxiliar para descobrir a "key" do campo MQL e os IDs das
    opcoes (Qualificado / Desqualificado). Acesse essa rota, procure
    o campo chamado "MQL" no resultado e copie o "key" pra variavel
    MQL_FIELD_KEY no Render, e o "id" da opcao "Qualificado" pra
    MQL_QUALIFICADO_ID.
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

    # O Pipedrive pode chamar o objeto atual de "current" ou "data"
    # dependendo da versao do webhook — tentamos os dois.
    atual = corpo.get("current") or corpo.get("data") or {}
    anterior = corpo.get("previous") or {}

    deal_id = atual.get("id", "desconhecido")

    mql_atual = extrair_valor_mql(atual)
    mql_anterior = extrair_valor_mql(anterior)

    logger.info(f"Negocio {deal_id} — MQL atual: {mql_atual} | MQL anterior: {mql_anterior}")

    if mql_atual != MQL_QUALIFICADO_ID:
        return {"status": "ignorado", "motivo": "MQL nao esta Qualificado", "mql_atual": mql_atual}

    if mql_anterior == MQL_QUALIFICADO_ID:
        return {"status": "ignorado", "motivo": "MQL ja estava Qualificado antes (evita duplicidade)"}

    person_id = extrair_person_id(atual)
    if not person_id:
        logger.warning(f"Negocio {deal_id}: sem pessoa vinculada, evento nao enviado")
        return {"status": "ignorado", "motivo": "negocio sem pessoa vinculada"}

    # Processa em background para responder ao Pipedrive rapido
    background_tasks.add_task(processar_negocio_qualificado, str(deal_id), person_id)

    return {"status": "recebido", "deal_id": deal_id}
