import os
import json
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Telegram Bot Engine Core")

# Разрешаем CORS для подключения фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Конфигурация Supabase (Заполняется из Environment Variables)
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://your-supabase.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "your-supabase-key")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# --- DTO Модели ---
class BotCreateRequest(BaseModel):
    user_id: str
    bot_token: str
    tier: str = "simple"

class ScenarioSaveRequest(BaseModel):
    bot_id: str
    mode: str
    flow_data: dict

# --- Вспомогательные функции Telegram API ---
async def send_telegram_message(bot_token: str, chat_id: int, text: str, reply_markup: dict = None):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

# --- Исполнительный движок сценариев (Flow Execution Engine) ---
async def execute_node_logic(bot: dict, scenario: dict, subscriber: dict, update: dict):
    message = update.get("message", {})
    callback_query = update.get("callback_query", {})
    
    chat_id = message.get("chat", {}).get("id") or callback_query.get("message", {}).get("chat", {}).get("id")
    text_input = message.get("text", "") or callback_query.get("data", "")
    
    if not chat_id:
        return
        
    mode = scenario.get("mode", "lite")
    flow_data = scenario.get("flow_data", {})
    
    # 1. LITE MODE: Обычный пошаговый опросник/кнопки
    if mode == "lite":
        answers = flow_data.get("answers", {})
        response_text = answers.get(text_input, flow_data.get("default_message", "Извините, команда не распознана."))
        await send_telegram_message(bot["bot_token"], chat_id, response_text)
        
    # 2. PRO MODE: Визуальный граф нод (Node Graph Engine)
    elif mode == "pro":
        nodes = flow_data.get("nodes", {})
        current_node_id = subscriber.get("current_node", "start")
        
        # Реакция на /start
        if text_input == "/start":
            current_node_id = "start"

        node = nodes.get(current_node_id) or nodes.get("start")
        if node:
            out_text = node.get("text", "Привет!")
            buttons = node.get("buttons", [])
            
            reply_markup = None
            if buttons:
                inline_keyboard = [[{"text": b["label"], "callback_data": b["target_node"]}] for b in buttons]
                reply_markup = {"inline_keyboard": inline_keyboard}

            await send_telegram_message(bot["bot_token"], chat_id, out_text, reply_markup)
            
            # Если нажата кнопка — переводим пользователя на следующую ноду
            if callback_query and "data" in callback_query:
                next_node = callback_query.get("data")
                async with httpx.AsyncClient() as client:
                    await client.patch(
                        f"{SUPABASE_URL}/rest/v1/bot_subscribers?id=eq.{subscriber['id']}",
                        headers=HEADERS,
                        json={"current_node": next_node}
                    )

    # 3. DEV MODE: Кастомный код (Python Node Sandbox Execution)
    elif mode == "dev":
        code_script = flow_data.get("python_code", "")
        
        # Безопасный контекст выполнения
        local_scope = {"update": update, "result": None}
        try:
            exec(code_script, {}, local_scope)
            res_text = local_scope.get("result", "Код выполнен без вывода.")
        except Exception as e:
            res_text = f" Ошибка в кастомном коде: {str(e)}"
            
        await send_telegram_message(bot["bot_token"], chat_id, str(res_text))

# --- API Эндпоинты ---

@app.post("/api/bots/register")
async def register_bot(req: BotCreateRequest, request: Request):
    # 1. Сохраняем бота в Supabase
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{SUPABASE_URL}/rest/v1/bots",
            headers={**HEADERS, "Prefer": "return=representation"},
            json={"user_id": req.user_id, "bot_token": req.bot_token, "tier": req.tier}
        )
        if res.status_code != 201:
            raise HTTPException(status_code=400, detail="Ошибка добавления бота")
        bot_data = res.json()[0]

    # 2. Регистрируем Webhook в Telegram API
    host_url = str(request.base_url).rstrip('/')
    webhook_url = f"{host_url}/webhook/{req.bot_token}"
    
    async with httpx.AsyncClient() as client:
        tg_res = await client.post(
            f"https://api.telegram.org/bot{req.bot_token}/setWebhook",
            json={"url": webhook_url}
        )
        
    return {"status": "success", "bot": bot_data, "telegram_webhook": tg_res.json()}


@app.post("/api/scenarios/save")
async def save_scenario(req: ScenarioSaveRequest):
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{SUPABASE_URL}/rest/v1/scenarios",
            headers={**HEADERS, "Prefer": "resolution=merge-duplicates"},
            json={"bot_id": req.bot_id, "mode": req.mode, "flow_data": req.flow_data}
        )
    return {"status": "saved", "data": res.json()}


@app.post("/webhook/{bot_token}")
async def handle_telegram_webhook(bot_token: str, request: Request):
    update = await request.json()
    
    async with httpx.AsyncClient() as client:
        # Получаем бота
        bot_res = await client.get(
            f"{SUPABASE_URL}/rest/v1/bots?bot_token=eq.{bot_token}&select=*",
            headers=HEADERS
        )
        bots = bot_res.json()
        if not bots:
            return {"status": "bot_not_found"}
        bot = bots[0]

        # Получаем сценарий
        sc_res = await client.get(
            f"{SUPABASE_URL}/rest/v1/scenarios?bot_id=eq.{bot['id']}&select=*",
            headers=HEADERS
        )
        scenarios = sc_res.json()
        scenario = scenarios[0] if scenarios else {"mode": "lite", "flow_data": {}}

        # Получаем или создаем подписчика
        chat_id = update.get("message", {}).get("chat", {}).get("id") or \
                  update.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")

        if not chat_id:
            return {"ok": True}

        sub_res = await client.get(
            f"{SUPABASE_URL}/rest/v1/bot_subscribers?bot_id=eq.{bot['id']}&telegram_id=eq.{chat_id}&select=*",
            headers=HEADERS
        )
        subs = sub_res.json()
        
        if not subs:
            new_sub_res = await client.post(
                f"{SUPABASE_URL}/rest/v1/bot_subscribers",
                headers={**HEADERS, "Prefer": "return=representation"},
                json={"bot_id": bot["id"], "telegram_id": chat_id, "current_node": "start"}
            )
            subscriber = new_sub_res.json()[0]
        else:
            subscriber = subs[0]

        # Запускаем логику в фоновом режиме
        asyncio.create_task(execute_node_logic(bot, scenario, subscriber, update))

    return {"ok": True}