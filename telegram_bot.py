# telegram_bot.py — Мультиаккаунт + PostgreSQL хранилище сессий
import os
import requests
import asyncpg
import json
from telethon.tl import functions, types
from telethon.errors import PeerIdInvalidError, UserIdInvalidError
from telethon.tl.types import InputMediaContact
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser, PeerChannel, PeerChat
from telethon.tl.functions.messages import GetDialogsRequest, GetDialogFiltersRequest
from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
from telethon.tl.types import InputPhoneContact
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneNumberInvalidError, UserPrivacyRestrictedError
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, validator
from contextlib import asynccontextmanager
from typing import List, Optional, Union, Dict
import uvicorn
from datetime import datetime
import base64

API_ID = 34135660
API_HASH = "c3cab94748a3618de8293a4a4f9cd571"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
DATABASE_URL = os.getenv("DATABASE_URL")  # Получаем из переменных окружения

# Хранилище: имя → клиент
ACTIVE_CLIENTS = {}
# Изменяем формат: добавляем флаг needs_2fa
PENDING_AUTH = {}  # Формат: {phone: {"session_str": "...", "phone_code_hash": "...", "needs_2fa": False}}

# ==================== КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ====================
class SessionDatabase:
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.pool = None
    
    async def connect(self):
        """Создаем пул соединений"""
        if not self.pool:
            try:
                self.pool = await asyncpg.create_pool(
                    self.connection_string,
                    min_size=1,
                    max_size=10
                )
                await self.create_table()
                print("✅ Подключение к PostgreSQL установлено")
            except Exception as e:
                print(f"❌ Ошибка подключения к PostgreSQL: {e}")
                raise
    
    async def create_table(self):
        """Создаем таблицу для хранения сессий"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS telegram_sessions (
                    id SERIAL PRIMARY KEY,
                    account_name VARCHAR(100) UNIQUE NOT NULL,
                    session_data TEXT NOT NULL,
                    phone_number VARCHAR(20),
                    user_id BIGINT,
                    first_name VARCHAR(100),
                    last_name VARCHAR(100),
                    username VARCHAR(100),
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_used TIMESTAMP DEFAULT NOW(),
                    is_active BOOLEAN DEFAULT TRUE,
                    metadata JSONB DEFAULT '{}'
                )
            ''')
            print("✅ Таблица сессий создана/проверена")
    
    async def save_session(self, 
                          account_name: str, 
                          session_string: str,
                          phone_number: Optional[str] = None,
                          user_id: Optional[int] = None,
                          first_name: Optional[str] = None,
                          last_name: Optional[str] = None,
                          username: Optional[str] = None):
        """Сохраняем или обновляем сессию"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO telegram_sessions 
                (account_name, session_data, phone_number, user_id, first_name, last_name, username, last_used)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                ON CONFLICT (account_name) 
                DO UPDATE SET
                session_data = EXCLUDED.session_data,
                phone_number = EXCLUDED.phone_number,
                user_id = EXCLUDED.user_id,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                username = EXCLUDED.username,
                last_used = NOW(),
                is_active = TRUE
            ''', account_name, session_string, phone_number, user_id, 
                first_name, last_name, username)
            print(f"✅ Сессия '{account_name}' сохранена в БД")
    
    async def get_session(self, account_name: str) -> Optional[str]:
        """Получаем сессию по имени аккаунта"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT session_data FROM telegram_sessions WHERE account_name = $1 AND is_active = TRUE',
                account_name
            )
            if row:
                # Обновляем время последнего использования
                await conn.execute(
                    'UPDATE telegram_sessions SET last_used = NOW() WHERE account_name = $1',
                    account_name
                )
                return row['session_data']
            return None
    
    async def list_sessions(self) -> List[Dict]:
        """Список всех сохраненных сессий"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT 
                    account_name, 
                    phone_number,
                    user_id,
                    first_name,
                    last_name,
                    username,
                    created_at,
                    last_used,
                    is_active
                FROM telegram_sessions 
                ORDER BY last_used DESC
            ''')
            return [dict(row) for row in rows]
    
    async def delete_session(self, account_name: str) -> bool:
        """Удаляем сессию"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                'DELETE FROM telegram_sessions WHERE account_name = $1',
                account_name
            )
            return "DELETE 1" in result
    
    async def update_metadata(self, account_name: str, metadata: Dict):
        """Обновляем метаданные аккаунта"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE telegram_sessions SET metadata = $2 WHERE account_name = $1',
                account_name,
                json.dumps(metadata)
            )
    
    async def deactivate_session(self, account_name: str):
        """Деактивируем сессию (помечаем как неактивную)"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE telegram_sessions SET is_active = FALSE WHERE account_name = $1',
                account_name
            )

# Инициализация базы данных
if DATABASE_URL:
    session_db = SessionDatabase(DATABASE_URL)
else:
    print("⚠️ DATABASE_URL не установлен. Сессии не будут сохраняться.")
    session_db = None

# ==================== Модели ====================
class SendMessageReq(BaseModel):
    account: str
    chat_id: str | int
    text: str

class AddAccountReq(BaseModel):
    name: str
    session_string: str

class RemoveAccountReq(BaseModel):
    name: str

class AuthStartReq(BaseModel):
    phone: str

class AuthCodeReq(BaseModel):
    phone: str
    code: str
    phone_code_hash: str
    password: str | None = None  # Опционально для 2FA

class Auth2FAReq(BaseModel):
    phone: str
    password: str  # Обязательно для 2FA

class ExportMembersReq(BaseModel):
    account: str
    group: str | int

# ==================== Новые модели ====================
class DialogInfo(BaseModel):
    id: int
    title: str
    username: Optional[str] = None
    folder_names: List[str] = []
    is_group: bool
    is_channel: bool
    is_user: bool
    unread_count: int
    last_message_date: Optional[str] = None

class GetDialogsReq(BaseModel):
    account: str
    limit: int = 50
    include_folders: bool = True

class ChatMessage(BaseModel):
    id: int
    date: str
    from_id: Optional[int] = None
    text: str
    is_outgoing: bool
    
    @validator('from_id', pre=True)
    def parse_from_id(cls, v):
        if v is None:
            return None
        if isinstance(v, (PeerUser, PeerChannel, PeerChat)):
            return v.user_id if isinstance(v, PeerUser) else v.channel_id if isinstance(v, PeerChannel) else v.chat_id
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
        return None

class GetChatHistoryReq(BaseModel):
    account: str
    chat_id: Union[str, int]
    limit: int = 50
    offset_id: Optional[int] = None

# ==================== НОВАЯ МОДЕЛЬ: отправка новым пользователям ====================
class SendToNewUserReq(BaseModel):
    account: str
    phone: str
    message: str
    first_name: str = "Contact"
    last_name: str = ""
    delete_after: bool = True

# ==================== НОВАЯ МОДЕЛЬ: добавление контакта ====================
class AddContactReq(BaseModel):
    account: str
    phone: str
    first_name: str = "Contact"
    last_name: str = ""

# ==================== НОВАЯ МОДЕЛЬ: отправка контакта ====================
class SendContactReq(BaseModel):
    account: str
    chat_id: Union[str, int]
    contact_id: Union[str, int]  # ID контакта для отправки
    first_name: str = ""  # Можно указать для уточнения
    last_name: str = ""  # Можно указать для уточнения
    phone: str = ""  # Можно указать для уточнения
    message: str = ""  # Опциональный текст сообщения с контактом

# ==================== НОВАЯ МОДЕЛЬ: Загрузка сессии ====================
class UploadSessionReq(BaseModel):
    account_name: str
    session_string: str
    activate_now: bool = True

# ==================== Вспомогательные функции ====================
def extract_folder_title(folder_obj):
    if not hasattr(folder_obj, 'title'):
        return None
    
    title_obj = folder_obj.title
    if hasattr(title_obj, 'text'):
        return title_obj.text
    elif isinstance(title_obj, str):
        return title_obj
    return None

async def get_dialogs_with_folders_info(client: TelegramClient, limit: int = 50) -> List[DialogInfo]:
    """Получить диалоги с информацией о папках"""
    try:
        folder_info = {}
        try:
            dialog_filters_result = await client(GetDialogFiltersRequest())
            dialog_filters = getattr(dialog_filters_result, 'filters', [])
            
            for folder in dialog_filters:
                folder_title = extract_folder_title(folder)
                
                if hasattr(folder, 'id') and folder_title:
                    folder_info[folder.id] = {
                        'title': folder_title,
                        'include_peers': [],
                        'exclude_peers': []
                    }
                    
                    if hasattr(folder, 'include_peers'):
                        for peer in folder.include_peers:
                            peer_id = None
                            if hasattr(peer, 'user_id'):
                                peer_id = peer.user_id
                            elif hasattr(peer, 'chat_id'):
                                peer_id = peer.chat_id
                            elif hasattr(peer, 'channel_id'):
                                peer_id = peer.channel_id
                            
                            if peer_id:
                                folder_info[folder.id]['include_peers'].append(peer_id)
        except Exception as e:
            print(f"Ошибка получения папок: {e}")
        
        dialogs = await client.get_dialogs(limit=limit)
        dialog_to_folders = {}
        
        for folder_id, folder_data in folder_info.items():
            for peer_id in folder_data['include_peers']:
                if peer_id not in dialog_to_folders:
                    dialog_to_folders[peer_id] = []
                dialog_to_folders[peer_id].append(folder_data['title'])
        
        dialog_list = []
        for dialog in dialogs:
            entity = dialog.entity
            folder_names = []
            dialog_id = entity.id
            
            if dialog_id in dialog_to_folders:
                folder_names = dialog_to_folders[dialog_id]
            
            dialog_info = DialogInfo(
                id=entity.id,
                title=dialog.title or dialog.name or "Без названия",
                username=getattr(entity, 'username', None),
                folder_names=folder_names,
                is_group=getattr(entity, 'megagroup', False) or getattr(entity, 'gigagroup', False),
                is_channel=getattr(entity, 'broadcast', False),
                is_user=hasattr(entity, 'first_name'),
                unread_count=dialog.unread_count,
                last_message_date=dialog.date.isoformat() if dialog.date else None
            )
            dialog_list.append(dialog_info)
        
        return dialog_list
        
    except Exception as e:
        print(f"Ошибка получения диалогов: {e}")
        dialogs = await client.get_dialogs(limit=limit)
        return [DialogInfo(
            id=dialog.entity.id,
            title=dialog.title or dialog.name or "Без названия",
            username=getattr(dialog.entity, 'username', None),
            folder_names=[],
            is_group=getattr(dialog.entity, 'megagroup', False) or getattr(dialog.entity, 'gigagroup', False),
            is_channel=getattr(dialog.entity, 'broadcast', False),
            is_user=hasattr(dialog.entity, 'first_name'),
            unread_count=dialog.unread_count,
            last_message_date=dialog.date.isoformat() if dialog.date else None
        ) for dialog in dialogs]

# ==================== Функция загрузки сессий при старте ====================
async def load_sessions_on_startup():
    """Загружаем все сохраненные сессии при старте"""
    if not session_db:
        print("⚠️ База данных не инициализирована. Пропускаем загрузку сессий.")
        return
    
    sessions = await session_db.list_sessions()
    print(f"🔍 Найдено {len(sessions)} сессий в базе данных")
    
    for session_info in sessions:
        account_name = session_info['account_name']
        
        try:
            session_string = await session_db.get_session(account_name)
            if not session_string:
                continue
            
            print(f"🔄 Загружаю аккаунт: {account_name}")
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await client.connect()
            
            if await client.is_user_authorized():
                await client.start()
                
                # Прогрев кэша
                try:
                    await client.get_dialogs(limit=20)
                except:
                    pass
                
                ACTIVE_CLIENTS[account_name] = client
                client.add_event_handler(
                    lambda event: incoming_handler(event),
                    events.NewMessage(incoming=True)
                )
                
                print(f"✅ Загружен аккаунт: {account_name}")
            else:
                await client.disconnect()
                print(f"❌ Невалидная сессия: {account_name}")
                # Помечаем как неактивную
                await session_db.deactivate_session(account_name)
                
        except Exception as e:
            print(f"❌ Ошибка загрузки сессии {account_name}: {e}")

# ==================== Lifespan ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Telegram Multi Gateway запущен")
    
    # Подключаемся к БД если есть URL
    if DATABASE_URL:
        try:
            await session_db.connect()
            print("✅ Подключение к PostgreSQL установлено")
            
            # Загружаем сохраненные сессии
            await load_sessions_on_startup()
            print(f"✅ Загружено {len(ACTIVE_CLIENTS)} аккаунтов")
            
        except Exception as e:
            print(f"❌ Ошибка инициализации БД: {e}")
    else:
        print("⚠️ DATABASE_URL не установлен. Работаем без сохранения сессий.")
    
    yield
    
    # Отключаем все аккаунты
    for client in ACTIVE_CLIENTS.values():
        await client.disconnect()
    print("Все аккаунты отключены")

app = FastAPI(title="Telegram Multi Account Gateway", lifespan=lifespan)

# ==================== Авторизация ====================
@app.post("/auth/start")
async def auth_start(req: AuthStartReq):
    """Начать авторизацию: запросить код подтверждения"""
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(req.phone)
        session_str = client.session.save()
        
        PENDING_AUTH[req.phone] = {
            "session_str": session_str,
            "phone_code_hash": sent_code.phone_code_hash,
            "needs_2fa": False
        }
        
        await client.disconnect()
        
        return {
            "status": "code_sent",
            "phone": req.phone,
            "phone_code_hash": sent_code.phone_code_hash,
            "needs_2fa": False
        }
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, detail=f"Ошибка: {str(e)}")

@app.post("/auth/complete")
async def auth_complete(req: AuthCodeReq):
    """
    Завершить авторизацию.
    Автоматически определяет нужен ли 2FA.
    """
    pending_data = PENDING_AUTH.get(req.phone)
    if not pending_data:
        raise HTTPException(400, "Нет активной авторизации")
    
    client = TelegramClient(StringSession(pending_data["session_str"]), API_ID, API_HASH)
    await client.connect()
    
    try:
        # 1. Пробуем войти с кодом
        try:
            await client.sign_in(
                phone=req.phone,
                code=req.code,
                phone_code_hash=pending_data["phone_code_hash"]
            )
            
        # 2. Если нужен пароль 2FA
        except SessionPasswordNeededError:
            # Обновляем статус в PENDING_AUTH
            PENDING_AUTH[req.phone]["needs_2fa"] = True
            
            # Если пароль уже предоставлен в этом же запросе
            if req.password:
                try:
                    await client.sign_in(password=req.password)
                except Exception as e:
                    await client.disconnect()
                    raise HTTPException(400, detail=f"Ошибка пароля 2FA: {str(e)}")
            else:
                await client.disconnect()
                # Возвращаем специальный статус для запроса пароля
                return {
                    "status": "2fa_required",
                    "phone": req.phone,
                    "needs_2fa": True,
                    "message": "Требуется пароль двухфакторной аутентификации",
                    "instructions": "Используйте /auth/2fa с параметром password"
                }
        
        # 3. Если другие ошибки с кодом
        except Exception as e:
            await client.disconnect()
            raise HTTPException(400, detail=f"Ошибка кода: {str(e)}")
        
        # 4. Если успешно (с кодом или кодом+паролем)
        session_str = client.session.save()
        del PENDING_AUTH[req.phone]
        await client.disconnect()
        
        return {
            "status": "success",
            "session_string": session_str,
            "message": "Авторизация успешна"
        }
        
    except Exception as e:
        await client.disconnect()
        raise HTTPException(500, detail=f"Неожиданная ошибка: {str(e)}")

@app.post("/auth/2fa")
async def auth_2fa(req: Auth2FAReq):
    """
    Отдельный эндпоинт для ввода пароля 2FA.
    Используется после получения статуса '2fa_required' от /auth/complete
    """
    pending_data = PENDING_AUTH.get(req.phone)
    if not pending_data:
        raise HTTPException(400, "Нет активной авторизации или сессия устарела")
    
    if not pending_data.get("needs_2fa", False):
        raise HTTPException(400, "Для этого номера не требуется 2FA")
    
    client = TelegramClient(StringSession(pending_data["session_str"]), API_ID, API_HASH)
    await client.connect()
    
    try:
        # Входим с паролем 2FA
        await client.sign_in(password=req.password)
        
        session_str = client.session.save()
        del PENDING_AUTH[req.phone]
        await client.disconnect()
        
        return {
            "status": "success",
            "session_string": session_str,
            "message": "2FA авторизация успешна"
        }
        
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, detail=f"Ошибка 2FA: {str(e)}")

# ==================== РАБОТА С СЕССИЯМИ В БАЗЕ ДАННЫХ ====================
@app.post("/sessions/upload")
async def upload_session(req: UploadSessionReq):
    """
    Загрузить сессию в базу данных
    """
    if not session_db:
        raise HTTPException(500, detail="База данных не инициализирована")
    
    try:
        # 1. Проверяем валидность сессии
        client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(400, detail="Невалидная сессия. Проверьте строку сессии.")
        
        # 2. Получаем информацию о пользователе
        me = await client.get_me()
        await client.disconnect()
        
        # 3. Сохраняем в БД
        await session_db.save_session(
            account_name=req.account_name,
            session_string=req.session_string,
            phone_number=getattr(me, 'phone', None),
            user_id=me.id,
            first_name=getattr(me, 'first_name', ''),
            last_name=getattr(me, 'last_name', ''),
            username=getattr(me, 'username', None)
        )
        
        result = {
            "status": "uploaded",
            "account": req.account_name,
            "user_id": me.id,
            "phone": getattr(me, 'phone', None),
            "username": getattr(me, 'username', None),
            "message": f"Сессия '{req.account_name}' сохранена в базе данных"
        }
        
        # 4. Если нужно активировать сразу
        if req.activate_now and req.account_name not in ACTIVE_CLIENTS:
            try:
                # Используем существующую функцию add_account
                client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
                await client.connect()
                await client.start()
                
                # Прогрев кэша
                try:
                    await client.get_dialogs(limit=20)
                except:
                    pass
                
                ACTIVE_CLIENTS[req.account_name] = client
                client.add_event_handler(
                    lambda event: incoming_handler(event),
                    events.NewMessage(incoming=True)
                )
                
                result["activated"] = True
                result["message"] = f"Сессия '{req.account_name}' сохранена и активирована"
                
            except Exception as e:
                result["activated"] = False
                result["activation_error"] = str(e)
        
        return result
        
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка загрузки сессии: {str(e)}")

@app.post("/sessions/upload_file")
async def upload_session_file(
    account_name: str = Form(...),
    session_file: UploadFile = File(...),
    activate_now: bool = Form(True)
):
    """
    Загрузить сессию из .session файла
    """
    if not session_db:
        raise HTTPException(500, detail="База данных не инициализирована")
    
    try:
        # 1. Читаем файл
        content = await session_file.read()
        
        # 2. Пробуем разные способы декодирования
        session_string = None
        
        # Способ 1: Прямое чтение как строки сессии
        try:
            session_string = content.decode('utf-8')
        except:
            pass
        
        # Способ 2: Base64 декодирование
        if not session_string:
            try:
                session_string = base64.b64encode(content).decode('utf-8')
            except:
                pass
        
        if not session_string:
            raise HTTPException(400, detail="Не удалось прочитать файл сессии")
        
        # 3. Используем существующий эндпоинт для загрузки
        return await upload_session(UploadSessionReq(
            account_name=account_name,
            session_string=session_string,
            activate_now=activate_now
        ))
        
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка загрузки файла: {str(e)}")

@app.get("/sessions/list")
async def list_sessions():
    """Список всех сохраненных сессий"""
    if not session_db:
        raise HTTPException(500, detail="База данных не инициализирована")
    
    try:
        sessions = await session_db.list_sessions()
        
        # Добавляем информацию о загруженных аккаунтах
        for session in sessions:
            session['is_loaded'] = session['account_name'] in ACTIVE_CLIENTS
        
        return {
            "status": "success",
            "total_sessions": len(sessions),
            "loaded_sessions": len(ACTIVE_CLIENTS),
            "sessions": sessions
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения списка: {str(e)}")

@app.post("/sessions/activate/{account_name}")
async def activate_session(account_name: str):
    """Активировать сессию из базы данных"""
    if not session_db:
        raise HTTPException(500, detail="База данных не инициализирована")
    
    if account_name in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Аккаунт {account_name} уже активен")
    
    try:
        session_string = await session_db.get_session(account_name)
        if not session_string:
            raise HTTPException(404, detail=f"Сессия {account_name} не найдена в базе данных")
        
        # Загружаем и проверяем сессию
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await client.disconnect()
            # Помечаем как неактивную
            await session_db.deactivate_session(account_name)
            raise HTTPException(400, detail="Сессия недействительна")
        
        await client.start()
        
        # Прогрев кэша
        try:
            dialogs = await client.get_dialogs(limit=50)
            print(f"Прогрет кэш для {account_name}: {len(dialogs)} чатов")
        except Exception as e:
            print(f"Ошибка прогрева кэша: {e}")
        
        ACTIVE_CLIENTS[account_name] = client
        client.add_event_handler(
            lambda event: incoming_handler(event),
            events.NewMessage(incoming=True)
        )
        
        return {
            "status": "activated",
            "account": account_name,
            "total_accounts": len(ACTIVE_CLIENTS)
        }
        
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка активации: {str(e)}")

@app.delete("/sessions/delete/{account_name}")
async def delete_session(account_name: str):
    """Удалить сессию из базы данных"""
    if not session_db:
        raise HTTPException(500, detail="База данных не инициализирована")
    
    try:
        # Отключаем аккаунт если он активен
        if account_name in ACTIVE_CLIENTS:
            client = ACTIVE_CLIENTS.pop(account_name)
            await client.disconnect()
        
        # Удаляем из БД
        deleted = await session_db.delete_session(account_name)
        
        if deleted:
            return {
                "status": "deleted",
                "account": account_name,
                "message": "Сессия удалена из базы данных"
            }
        else:
            raise HTTPException(404, detail=f"Сессия {account_name} не найдена")
            
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка удаления: {str(e)}")

# ==================== Работа с аккаунтами (обновленная) ====================
@app.post("/accounts/add")
async def add_account(req: AddAccountReq):
    """
    Добавить аккаунт (совместимость со старым API)
    """
    if req.name in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Аккаунт {req.name} уже существует")
    
    # Сохраняем сессию в БД если она инициализирована
    if session_db:
        try:
            client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
            await client.connect()
            
            if not await client.is_user_authorized():
                await client.disconnect()
                raise HTTPException(400, detail="Сессия недействительна")
            
            me = await client.get_me()
            await client.disconnect()
            
            await session_db.save_session(
                account_name=req.name,
                session_string=req.session_string,
                phone_number=getattr(me, 'phone', None),
                user_id=me.id,
                first_name=getattr(me, 'first_name', ''),
                last_name=getattr(me, 'last_name', ''),
                username=getattr(me, 'username', None)
            )
        except Exception as e:
            print(f"⚠️ Не удалось сохранить сессию в БД: {e}")
    
    # Остальной код без изменений
    client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
    await client.connect()
    
    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(400, detail="Сессия недействительна")
    
    await client.start()
    
    try:
        dialogs = await client.get_dialogs(limit=50)
        print(f"Прогрет кэш для {req.name}: {len(dialogs)} чатов")
    except Exception as e:
        print(f"Ошибка прогрева кэша: {e}")
    
    ACTIVE_CLIENTS[req.name] = client
    client.add_event_handler(
        lambda event: incoming_handler(event),
        events.NewMessage(incoming=True)
    )
    
    return {
        "status": "added",
        "account": req.name,
        "total_accounts": len(ACTIVE_CLIENTS),
        "saved_to_db": session_db is not None
    }

@app.delete("/accounts/{name}")
async def remove_account(name: str):
    client = ACTIVE_CLIENTS.pop(name, None)
    if client:
        await client.disconnect()
        return {"status": "removed", "account": name}
    raise HTTPException(404, detail="Аккаунт не найден")

@app.get("/accounts")
def list_accounts():
    return {"active_accounts": list(ACTIVE_CLIENTS.keys())}

# ==================== Веб-интерфейс для загрузки ====================
from fastapi.responses import HTMLResponse

@app.get("/upload", response_class=HTMLResponse)
async def upload_form():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Загрузка Telegram сессий</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            .container { max-width: 600px; margin: 0 auto; }
            .form-group { margin-bottom: 20px; }
            label { display: block; margin-bottom: 5px; font-weight: bold; }
            input[type="text"], input[type="file"] {
                width: 100%;
                padding: 10px;
                margin-bottom: 10px;
                border: 1px solid #ddd;
                border-radius: 4px;
            }
            button {
                background: #007bff;
                color: white;
                border: none;
                padding: 12px 24px;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
            }
            button:hover { background: #0056b3; }
            .result { margin-top: 20px; padding: 15px; border-radius: 4px; }
            .success { background: #d4edda; color: #155724; }
            .error { background: #f8d7da; color: #721c24; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📁 Загрузка Telegram сессии</h1>
            
            <form id="uploadForm" enctype="multipart/form-data">
                <div class="form-group">
                    <label for="account_name">Имя аккаунта:</label>
                    <input type="text" id="account_name" name="account_name" required 
                           placeholder="Например: my_account">
                </div>
                
                <div class="form-group">
                    <label for="session_file">.session файл:</label>
                    <input type="file" id="session_file" name="session_file" accept=".session" required>
                </div>
                
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="activate_now" name="activate_now" checked>
                        Активировать сразу после загрузки
                    </label>
                </div>
                
                <button type="submit">📤 Загрузить сессию</button>
            </form>
            
            <div id="result" class="result" style="display: none;"></div>
            
            <script>
                document.getElementById('uploadForm').addEventListener('submit', async function(e) {
                    e.preventDefault();
                    
                    const formData = new FormData();
                    formData.append('account_name', document.getElementById('account_name').value);
                    formData.append('session_file', document.getElementById('session_file').files[0]);
                    formData.append('activate_now', document.getElementById('activate_now').checked);
                    
                    const resultDiv = document.getElementById('result');
                    resultDiv.style.display = 'block';
                    resultDiv.textContent = 'Загрузка...';
                    resultDiv.className = 'result';
                    
                    try {
                        const response = await fetch('/sessions/upload_file', {
                            method: 'POST',
                            body: formData
                        });
                        
                        const data = await response.json();
                        
                        if (response.ok) {
                            resultDiv.className = 'result success';
                            resultDiv.innerHTML = `
                                <h3>✅ Успешно!</h3>
                                <p>Аккаунт: <strong>${data.account}</strong></p>
                                <p>ID: ${data.user_id}</p>
                                ${data.phone ? `<p>Телефон: ${data.phone}</p>` : ''}
                                ${data.username ? `<p>Username: @${data.username}</p>` : ''}
                                <p>${data.message}</p>
                                ${data.activated ? '<p>🟢 Аккаунт активирован</p>' : ''}
                            `;
                        } else {
                            resultDiv.className = 'result error';
                            resultDiv.textContent = 'Ошибка: ' + (data.detail || 'Неизвестная ошибка');
                        }
                    } catch (error) {
                        resultDiv.className = 'result error';
                        resultDiv.textContent = 'Ошибка сети: ' + error.message;
                    }
                });
            </script>
        </div>
    </body>
    </html>
    """

# ==================== Остальные эндпоинты (без изменений) ====================
async def incoming_handler(event):
    if event.is_outgoing:
        return

    from_account = "unknown"
    for name, cl in ACTIVE_CLIENTS.items():
        if cl.session == event.client.session:
            from_account = name
            break

    payload = {
        "from_account": from_account,
        "sender_id": event.sender_id,
        "chat_id": event.chat_id,
        "message_id": event.id,
        "text": event.text or "",
        "date": event.date.isoformat() if event.date else None,
    }

    if WEBHOOK_URL:
        try:
            requests.post(WEBHOOK_URL, json=payload, timeout=12)
        except:
            pass

@app.post("/send")
async def send_message(req: SendMessageReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        await client.send_message(req.chat_id, req.text)
        return {"status": "sent", "from": req.account, "to": req.chat_id}
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка отправки: {str(e)}")

@app.post("/export_members")
async def export_members(req: ExportMembersReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        group = await client.get_entity(req.group)
        participants = await client.get_participants(group, aggressive=True)

        members = []
        for p in participants:
            # Определяем, является ли участник администратором
            is_admin = False
            admin_title = None
            
            # Проверяем разные способы определения администратора
            if hasattr(p, 'participant'):
                # Для участников групп/каналов
                participant = p.participant
                if hasattr(participant, 'admin_rights') and participant.admin_rights:
                    is_admin = True
                    admin_title = getattr(participant, 'rank', None) or getattr(participant, 'title', None)
            
            # Альтернативная проверка через права
            if not is_admin and hasattr(p, 'admin_rights') and p.admin_rights:
                is_admin = True
            
            # Собираем информацию об участнике
            member_data = {
                "id": p.id,
                "username": p.username if hasattr(p, 'username') and p.username else None,
                "first_name": p.first_name if hasattr(p, 'first_name') and p.first_name else "",
                "last_name": p.last_name if hasattr(p, 'last_name') and p.last_name else "",
                "phone": p.phone if hasattr(p, 'phone') and p.phone else None,
                "is_admin": is_admin,
                "admin_title": admin_title,
                "is_bot": p.bot if hasattr(p, 'bot') else False,
                "is_self": p.self if hasattr(p, 'self') else False,
                "is_contact": p.contact if hasattr(p, 'contact') else False,
                "is_mutual_contact": p.mutual_contact if hasattr(p, 'mutual_contact') else False,
                "is_deleted": p.deleted if hasattr(p, 'deleted') else False,
                "is_verified": p.verified if hasattr(p, 'verified') else False,
                "is_restricted": p.restricted if hasattr(p, 'restricted') else False,
                "is_scam": p.scam if hasattr(p, 'scam') else False,
                "is_fake": p.fake if hasattr(p, 'fake') else False,
                "is_support": p.support if hasattr(p, 'support') else False,
                "is_premium": p.premium if hasattr(p, 'premium') else False,
            }
            
            # Добавляем статус (онлайн/офлайн)
            if hasattr(p, 'status'):
                status = p.status
                if hasattr(status, '__class__'):
                    member_data["status"] = status.__class__.__name__
                    if hasattr(status, 'was_online'):
                        member_data["last_seen"] = status.was_online.isoformat() if status.was_online else None
            
            members.append(member_data)

        return {
            "status": "exported",
            "group": req.group,
            "group_title": group.title if hasattr(group, 'title') else "Unknown",
            "total_members": len(members),
            "admins_count": sum(1 for m in members if m["is_admin"]),
            "bots_count": sum(1 for m in members if m["is_bot"]),
            "members": members
        }
    except Exception as e:
        print(f"Ошибка экспорта участников: {e}")
        raise HTTPException(500, detail=f"Ошибка экспорта: {str(e)}")

@app.post("/dialogs")
async def get_dialogs(req: GetDialogsReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        if req.include_folders:
            dialog_list = await get_dialogs_with_folders_info(client, req.limit)
        else:
            dialogs = await client.get_dialogs(limit=req.limit)
            dialog_list = [
                DialogInfo(
                    id=dialog.entity.id,
                    title=dialog.title or dialog.name or "Без названия",
                    username=getattr(dialog.entity, 'username', None),
                    folder_names=[],
                    is_group=getattr(dialog.entity, 'megagroup', False) or getattr(dialog.entity, 'gigagroup', False),
                    is_channel=getattr(dialog.entity, 'broadcast', False),
                    is_user=hasattr(dialog.entity, 'first_name'),
                    unread_count=dialog.unread_count,
                    last_message_date=dialog.date.isoformat() if dialog.date else None
                ) for dialog in dialogs
            ]
        
        return {
            "status": "success",
            "account": req.account,
            "total_dialogs": len(dialog_list),
            "dialogs": dialog_list
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения диалогов: {str(e)}")

# ==================== Остальные эндпоинты оставлены без изменений ====================
# (send_to_new_user, add_contact, send_contact, send_contact_simple, folders, chat_history)

# ==================== Запуск ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, reload=False)
