# telegram_bot.py — Мультиаккаунт + экспорт участников группы + мгновенная работа с любыми ID
import os
import requests
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
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, validator
from contextlib import asynccontextmanager
from typing import List, Optional, Union, Dict
import uvicorn
from datetime import datetime

API_ID = 34135660
API_HASH = "c3cab94748a3618de8293a4a4f9cd571"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Хранилище: имя → клиент
ACTIVE_CLIENTS = {}
# Изменяем формат: добавляем флаг needs_2fa
PENDING_AUTH = {}  # Формат: {phone: {"session_str": "...", "phone_code_hash": "...", "needs_2fa": False}}


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

# ==================== НОВАЯ МОДЕЛЬ: Получить информацию об отправителе ====================
class GetSenderInfoReq(BaseModel):
    account: str
    chat_id: Union[str, int]
    message_id: int

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


# ==================== Lifespan ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Telegram Multi Gateway запущен")
    yield
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


# ==================== Работа с аккаунтами ====================
@app.post("/accounts/add")
async def add_account(req: AddAccountReq):
    if req.name in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Аккаунт {req.name} уже существует")

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
        "total_accounts": len(ACTIVE_CLIENTS)
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


# ==================== НОВЫЙ ЭНДПОИНТ: Получить информацию об отправителе сообщения ====================
@app.post("/get_sender_info")
async def get_sender_info(req: GetSenderInfoReq):
    """
    Получить информацию об отправителе сообщения по его ID.
    Возвращает полную информацию о пользователе, который отправил сообщение.
    """
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        # 1. Получаем сущность чата
        chat = await client.get_entity(req.chat_id)
        
        # 2. Получаем сообщение по ID
        messages = await client.get_messages(
            entity=chat,
            ids=req.message_id
        )
        
        if not messages or (isinstance(messages, list) and len(messages) == 0):
            raise HTTPException(404, detail=f"Сообщение с ID {req.message_id} не найдено")
        
        message = messages[0] if isinstance(messages, list) else messages
        
        # 3. Получаем информацию об отправителе
        sender = None
        sender_id = None
        
        # Определяем ID отправителя
        if hasattr(message, 'from_id'):
            if message.from_id:
                # Получаем из from_id
                from_id = message.from_id
                if hasattr(from_id, 'user_id'):
                    sender_id = from_id.user_id
                elif hasattr(from_id, 'channel_id'):
                    sender_id = from_id.channel_id
                elif hasattr(from_id, 'chat_id'):
                    sender_id = from_id.chat_id
        
        # Альтернативный способ
        if not sender_id and hasattr(message, 'sender_id'):
            sender_id = message.sender_id
        
        # Если есть ID отправителя, получаем его информацию
        if sender_id:
            try:
                sender = await client.get_entity(sender_id)
            except Exception as e:
                print(f"⚠️ Не удалось получить информацию об отправителе: {e}")
        
        # Если не удалось получить по ID, пробуем получить из sender
        if not sender and hasattr(message, 'sender'):
            sender = message.sender
        
        # Если отправитель не найден
        if not sender:
            raise HTTPException(404, detail="Информация об отправителе не найдена")
        
        # 4. Формируем информацию об отправителе
        sender_info = {
            "id": sender.id,
            "first_name": getattr(sender, 'first_name', ''),
            "last_name": getattr(sender, 'last_name', ''),
            "username": getattr(sender, 'username', None),
            "phone": getattr(sender, 'phone', None),
            "is_bot": getattr(sender, 'bot', False),
            "is_premium": getattr(sender, 'premium', False),
            "is_verified": getattr(sender, 'verified', False),
            "is_restricted": getattr(sender, 'restricted', False),
            "is_scam": getattr(sender, 'scam', False),
            "is_fake": getattr(sender, 'fake', False),
            "is_support": getattr(sender, 'support', False),
            "is_contact": getattr(sender, 'contact', False),
            "is_deleted": getattr(sender, 'deleted', False),
            "is_self": getattr(sender, 'self', False),
            "is_mutual_contact": getattr(sender, 'mutual_contact', False),
        }
        
        # 5. Добавляем информацию о чате (если отправитель - группа или канал)
        if hasattr(sender, 'title'):
            sender_info["title"] = sender.title
            sender_info["is_channel"] = getattr(sender, 'broadcast', False)
            sender_info["is_group"] = getattr(sender, 'megagroup', False) or getattr(sender, 'gigagroup', False)
            sender_info["participants_count"] = getattr(sender, 'participants_count', None)
        
        # 6. Добавляем статус (онлайн/офлайн)
        if hasattr(sender, 'status'):
            status = sender.status
            if hasattr(status, '__class__'):
                sender_info["status"] = status.__class__.__name__
                if hasattr(status, 'was_online'):
                    sender_info["last_seen"] = status.was_online.isoformat() if status.was_online else None
                if hasattr(status, 'expires'):
                    sender_info["status_expires"] = status.expires.isoformat() if status.expires else None
        
        # 7. Добавляем информацию о самом сообщении
        message_info = {
            "id": message.id,
            "date": message.date.isoformat() if hasattr(message, 'date') and message.date else None,
            "text": message.text if hasattr(message, 'text') else message.message if hasattr(message, 'message') else "",
            "is_outgoing": message.out if hasattr(message, 'out') else False,
            "is_forward": message.forward if hasattr(message, 'forward') else False,
            "has_media": bool(message.media) if hasattr(message, 'media') else False,
            "has_reply": bool(message.reply_to) if hasattr(message, 'reply_to') else False,
        }
        
        return {
            "status": "success",
            "account": req.account,
            "message_id": req.message_id,
            "chat_id": req.chat_id,
            "chat_title": getattr(chat, 'title', getattr(chat, 'first_name', 'Unknown')),
            "sender": sender_info,
            "message": message_info,
            "timestamp": datetime.now().isoformat(),
            "note": "Информация получена успешно"
        }
        
    except PeerIdInvalidError:
        raise HTTPException(400, detail="Неверный ID чата или пользователя")
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Ошибка получения информации об отправителе: {error_msg}")
        
        if "MESSAGE_ID_INVALID" in error_msg:
            raise HTTPException(404, detail=f"Сообщение с ID {req.message_id} не найдено в указанном чате")
        elif "CHANNEL_PRIVATE" in error_msg:
            raise HTTPException(403, detail="Нет доступа к указанному каналу")
        elif "CHAT_FORBIDDEN" in error_msg:
            raise HTTPException(403, detail="Нет доступа к указанному чату")
        else:
            raise HTTPException(500, detail=f"Ошибка получения информации об отправителе: {error_msg}")


# ==================== НОВЫЙ ЭНДПОИНТ: Отправка сообщения новому пользователю ====================
@app.post("/send_to_new_user")
async def send_to_new_user(req: SendToNewUserReq):
    """
    Отправить сообщение пользователю, которого нет в контактах.
    Бот автоматически добавит пользователя в контакты, отправит сообщение
    и при необходимости удалит из контактов.
    """
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        # 1. Добавляем пользователя в контакты
        print(f"📇 Добавляю в контакты: {req.phone}")
        
        contact = InputPhoneContact(
            client_id=0,  # 0 для автоматического ID
            phone=req.phone,
            first_name=req.first_name,
            last_name=req.last_name
        )
        
        result = await client(ImportContactsRequest([contact]))
        
        if not result.users:
            raise HTTPException(400, detail=f"Пользователь не найден по номеру {req.phone}")
        
        user = result.users[0]
        print(f"✅ Успешно добавлен! ID: {user.id}, Имя: {user.first_name}")
        
        # 2. Отправляем сообщение
        print(f"📤 Отправляю сообщение пользователю {user.id}...")
        
        try:
            await client.send_message(user, req.message)
            print(f"✅ Сообщение отправлено!")
            
            # 3. Удаляем из контактов если требуется
            if req.delete_after:
                print(f"🗑️ Удаляю из контактов...")
                await client(DeleteContactsRequest(id=[user]))
                print(f"✅ Удалено из контактов")
            
            return {
                "status": "sent",
                "account": req.account,
                "phone": req.phone,
                "user_id": user.id,
                "user_info": {
                    "first_name": user.first_name,
                    "last_name": user.last_name or "",
                    "username": getattr(user, 'username', None)
                },
                "deleted_from_contacts": req.delete_after,
                "message_preview": req.message[:100] + "..." if len(req.message) > 100 else req.message
            }
            
        except FloodWaitError as e:
            print(f"⏳ Ограничение Telegram: ждите {e.seconds} секунд")
            # Удаляем пользователя из контактов, чтобы не оставлять следов
            if not req.delete_after:
                try:
                    await client(DeleteContactsRequest(id=[user]))
                except:
                    pass
            raise HTTPException(429, detail=f"Ограничение Telegram: ждите {e.seconds} секунд")
            
        except UserPrivacyRestrictedError:
            print(f"❌ Пользователь запретил получение сообщений")
            # Удаляем пользователя из контактов
            if not req.delete_after:
                try:
                    await client(DeleteContactsRequest(id=[user]))
                except:
                    pass
            raise HTTPException(403, detail="Пользователь запретил получение сообщений")
            
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")
            # Удаляем пользователя из контактов в случае ошибки
            if not req.delete_after:
                try:
                    await client(DeleteContactsRequest(id=[user]))
                except:
                    pass
            raise HTTPException(500, detail=f"Ошибка отправки сообщения: {str(e)}")
            
    except PhoneNumberInvalidError:
        raise HTTPException(400, detail=f"Некорректный номер телефона: {req.phone}. Формат должен быть: +79991234567")
        
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка обработки: {str(e)}")


# ==================== НОВЫЙ ЭНДПОИНТ: Добавить контакт ====================
@app.post("/add_contact")
async def add_contact(req: AddContactReq):
    """
    Добавить контакт по номеру телефона.
    Возвращает информацию о добавленном пользователе.
    """
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        # 1. Добавляем пользователя в контакты
        print(f"📇 Добавляю контакт: {req.phone}")
        
        contact = InputPhoneContact(
            client_id=0,  # 0 для автоматического ID
            phone=req.phone,
            first_name=req.first_name,
            last_name=req.last_name
        )
        
        result = await client(ImportContactsRequest([contact]))
        
        if not result.users:
            raise HTTPException(400, detail=f"Пользователь не найден по номеру {req.phone}. "
                                         "Проверьте корректность номера и что пользователь существует в Telegram.")
        
        user = result.users[0]
        print(f"✅ Контакт успешно добавлен! ID: {user.id}, Имя: {user.first_name}")
        
        # 2. Получаем полную информацию о пользователе
        user_info = {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name or "",
            "username": getattr(user, 'username', None),
            "phone": req.phone,
            "bot": getattr(user, 'bot', False),
            "premium": getattr(user, 'premium', False),
            "verified": getattr(user, 'verified', False),
            "restricted": getattr(user, 'restricted', False),
            "scam": getattr(user, 'scam', False),
            "access_hash": user.access_hash if hasattr(user, 'access_hash') else None
        }
        
        # 3. Проверяем, есть ли у пользователя ограничения на отправку сообщений
        can_message = True
        try:
            # Пробуем отправить тестовое сообщение (не отправляем на самом деле)
            if hasattr(user, 'bot') and user.bot:
                can_message = True
            else:
                # Проверяем возможность отправки сообщений через get_entity
                await client.get_entity(user.id)
        except UserPrivacyRestrictedError:
            can_message = False
        except Exception:
            can_message = True
        
        return {
            "status": "contact_added",
            "account": req.account,
            "phone": req.phone,
            "contact": user_info,
            "metadata": {
                "can_message": can_message,
                "in_contacts": True,
                "date_added": datetime.now().isoformat(),
                "imported_count": result.imported[0] if hasattr(result, 'imported') and result.imported else 1
            },
            "message": f"Контакт '{req.first_name} {req.last_name}' успешно добавлен"
        }
        
    except PhoneNumberInvalidError:
        raise HTTPException(400, detail=f"Некорректный номер телефона: {req.phone}. "
                                     "Формат должен быть: +79991234567 (с кодом страны)")
        
    except FloodWaitError as e:
        raise HTTPException(429, detail=f"Ограничение Telegram: подождите {e.seconds} секунд перед повторной попыткой")
        
    except Exception as e:
        error_msg = str(e)
        if "PHONE_NOT_OCCUPIED" in error_msg:
            raise HTTPException(400, detail=f"Номер {req.phone} не зарегистрирован в Telegram")
        elif "PHONE_NUMBER_BANNED" in error_msg:
            raise HTTPException(400, detail=f"Номер {req.phone} заблокирован в Telegram")
        elif "PHONE_NUMBER_FLOOD" in error_msg:
            raise HTTPException(429, detail="Слишком много запросов добавления контактов. Подождите некоторое время.")
        else:
            raise HTTPException(500, detail=f"Ошибка добавления контакта: {error_msg}")


# ==================== НОВЫЙ ЭНДПОИНТ: Отправить контакт (рабочий способ) ====================
@app.post("/send_contact")
async def send_contact(req: SendContactReq):
    """
    Отправить контакт как вложение.
    Работает через прямой вызов messages.SendMessageRequest.
    """
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        print(f"🔍 Получаю информацию о контакте: {req.contact_id}")
        
        # 1. Получаем информацию о контакте
        try:
            if isinstance(req.contact_id, (str, int)):
                contact_entity = await client.get_entity(req.contact_id)
            else:
                contact_entity = req.contact_id
        except Exception as e:
            raise HTTPException(400, detail=f"Не удалось найти контакт: {str(e)}")
        
        # 2. Получаем данные контакта
        contact_id = getattr(contact_entity, 'id', None)
        if not contact_id:
            raise HTTPException(400, detail="Не удалось получить ID контакта")
        
        # Получаем данные из entity или используем предоставленные
        contact_first_name = req.first_name or getattr(contact_entity, 'first_name', '')
        contact_last_name = req.last_name or getattr(contact_entity, 'last_name', '')
        contact_phone = req.phone or getattr(contact_entity, 'phone', '')
        
        # 3. Если нет телефона, ищем в списке контактов
        if not contact_phone:
            try:
                contacts = await client.get_contacts()
                for contact in contacts:
                    if contact.id == contact_id:
                        contact_phone = getattr(contact, 'phone', '')
                        # Если не указаны имя/фамилия, берем из контакта
                        if not contact_first_name:
                            contact_first_name = getattr(contact, 'first_name', 'Контакт')
                        if not contact_last_name:
                            contact_last_name = getattr(contact, 'last_name', '')
                        break
            except Exception as e:
                print(f"⚠️ Ошибка при получении списка контактов: {e}")
        
        # 4. Проверяем обязательные поля
        if not contact_phone:
            raise HTTPException(400, detail="Номер телефона контакта не найден. "
                                         "Укажите параметр 'phone' или добавьте контакт через /add_contact")
        
        if not contact_first_name:
            contact_first_name = "Контакт"
        
        print(f"📤 Отправляю контакт: {contact_first_name} {contact_last_name} ({contact_phone})")
        
        # 5. Получаем сущность чата для отправки
        try:
            chat_entity = await client.get_entity(req.chat_id)
        except Exception as e:
            raise HTTPException(400, detail=f"Не удалось найти чат: {str(e)}")
        
        # 6. Создаем InputMediaContact
        media_contact = types.InputMediaContact(
            phone_number=contact_phone,
            first_name=contact_first_name,
            last_name=contact_last_name,
            vcard=''  # Можно оставить пустым
        )
        
        # 7. Отправляем контакт через прямой запрос
        result = await client(functions.messages.SendMessageRequest(
            peer=chat_entity,
            message=req.message if req.message else "",  # Текст сообщения (может быть пустым)
            media=media_contact,
            silent=None,
            background=None,
            clear_draft=None,
            reply_to=None,
            schedule_date=None,
            send_as=None
        ))
        
        # 8. Получаем ID отправленного сообщения
        message_id = None
        if hasattr(result, 'updates'):
            for update in result.updates:
                if hasattr(update, 'id'):
                    message_id = update.id
                    break
        elif hasattr(result, 'id'):
            message_id = result.id
        
        print(f"✅ Контакт успешно отправлен! ID сообщения: {message_id}")
        
        return {
            "status": "success",
            "account": req.account,
            "chat_id": req.chat_id,
            "contact": {
                "id": contact_id,
                "first_name": contact_first_name,
                "last_name": contact_last_name,
                "phone": contact_phone,
                "username": getattr(contact_entity, 'username', None)
            },
            "message": {
                "id": message_id,
                "text": req.message,
                "has_caption": bool(req.message)
            },
            "timestamp": datetime.now().isoformat(),
            "method": "SendMessageRequest"
        }
        
    except PeerIdInvalidError:
        raise HTTPException(400, detail="Неверный ID чата или пользователя")
    except UserIdInvalidError:
        raise HTTPException(400, detail="Неверный ID пользователя")
    except FloodWaitError as e:
        raise HTTPException(429, detail=f"Ограничение Telegram: подождите {e.seconds} секунд")
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Ошибка отправки контакта: {error_msg}")
        
        # Обработка специфических ошибок
        if "PHONE_NUMBER_INVALID" in error_msg:
            raise HTTPException(400, detail="Неверный формат номера телефона")
        elif "PHONE_NOT_OCCUPIED" in error_msg:
            raise HTTPException(400, detail="Номер телефона не зарегистрирован в Telegram")
        elif "USER_PRIVACY_RESTRICTED" in error_msg:
            raise HTTPException(403, detail="Пользователь ограничил получение сообщений")
        elif "CHAT_WRITE_FORBIDDEN" in error_msg:
            raise HTTPException(403, detail="Нет прав на отправку сообщений в этот чат")
        else:
            raise HTTPException(500, detail=f"Ошибка отправки контакта: {error_msg}")


# ==================== НОВЫЙ ЭНДПОИНТ: Отправить контакт (самый простой способ) ====================
@app.post("/send_contact_simple")
async def send_contact_simple(req: SendContactReq):
    """
    Самый простой способ отправить контакт.
    Требует явного указания телефона, имени и фамилии.
    """
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        # 1. Проверяем обязательные поля
        if not req.phone:
            raise HTTPException(400, detail="Параметр 'phone' обязателен")
        if not req.first_name:
            raise HTTPException(400, detail="Параметр 'first_name' обязателен")
        
        # 2. Получаем сущность чата
        chat_entity = await client.get_entity(req.chat_id)
        
        # 3. Создаем InputMediaContact
        from telethon.tl.types import InputMediaContact
        
        media_contact = InputMediaContact(
            phone_number=req.phone,
            first_name=req.first_name,
            last_name=req.last_name,
            vcard=''
        )
        
        # 4. Отправляем сообщение
        result = await client.send_message(
            entity=chat_entity,
            message=req.message if req.message else "",
            file=media_contact
        )
        
        return {
            "status": "success",
            "account": req.account,
            "chat_id": req.chat_id,
            "contact": {
                "phone": req.phone,
                "first_name": req.first_name,
                "last_name": req.last_name
            },
            "message": {
                "id": result.id,
                "text": req.message
            }
        }
        
    except Exception as e:
        error_msg = str(e)
        raise HTTPException(500, detail=f"Ошибка отправки контакта: {error_msg}")
   
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


@app.post("/folders/{account}")
async def get_all_folders(account: str):
    client = ACTIVE_CLIENTS.get(account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {account}")

    try:
        dialog_filters_result = await client(GetDialogFiltersRequest())
        dialog_filters = getattr(dialog_filters_result, 'filters', [])
        folders = []
        
        for folder in dialog_filters:
            folder_title = extract_folder_title(folder)
            
            if hasattr(folder, 'id') and folder_title:
                folder_info = {
                    "id": folder.id,
                    "title": folder_title,
                    "color": getattr(folder, 'color', None),
                    "pinned": getattr(folder, 'pinned', False),
                    "include_count": len(getattr(folder, 'include_peers', [])),
                    "exclude_count": len(getattr(folder, 'exclude_peers', []))
                }
                folders.append(folder_info)
        
        return {
            "status": "success",
            "account": account,
            "total_folders": len(folders),
            "folders": folders
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения папок: {str(e)}")


@app.post("/chat_history")
async def get_chat_history(req: GetChatHistoryReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        chat_id = req.chat_id
        
        if isinstance(chat_id, str):
            if chat_id.startswith('@'):
                chat_id = chat_id[1:]
            if chat_id.lstrip('-').isdigit():
                chat_id = int(chat_id)
        
        try:
            chat = await client.get_entity(chat_id)
        except Exception:
            dialogs = await client.get_dialogs()
            for dialog in dialogs:
                if str(dialog.id) == str(chat_id) or (hasattr(dialog.entity, 'username') and dialog.entity.username == chat_id):
                    chat = dialog.entity
                    break
            else:
                raise HTTPException(400, detail=f"Не удалось найти чат: {req.chat_id}")
        
        messages = await client.get_messages(
            chat,
            limit=req.limit,
            offset_id=req.offset_id if req.offset_id and req.offset_id > 0 else None
        )
        
        message_list = []
        for msg in messages:
            if msg is None:
                continue
                
            text = ""
            if hasattr(msg, 'text') and msg.text:
                text = msg.text
            elif hasattr(msg, 'message') and msg.message:
                text = msg.message
            
            if not text and not hasattr(msg, 'media'):
                continue
            
            message = ChatMessage(
                id=msg.id,
                date=msg.date.isoformat() if msg.date else "",
                from_id=None,
                text=text,
                is_outgoing=msg.out if hasattr(msg, 'out') else False
            )
            message_list.append(message)
        
        chat_title = "Unknown"
        if hasattr(chat, 'title'):
            chat_title = chat.title
        elif hasattr(chat, 'first_name'):
            chat_title = chat.first_name
            if hasattr(chat, 'last_name') and chat.last_name:
                chat_title += f" {chat.last_name}"
        
        return {
            "status": "success",
            "account": req.account,
            "chat_id": req.chat_id,
            "chat_title": chat_title,
            "total_messages": len(message_list),
            "messages": message_list
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения истории: {str(e)}")


# ==================== Запуск ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, reload=False)
