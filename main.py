import os
import logging
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from dotenv import load_dotenv
import pymongo

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)

# Initialize bot and dispatcher
bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# MongoDB connection
mongo_client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = mongo_client["telegram_twilio_bot"]
users_collection = db["users"]
credentials_collection = db["credentials"]
numbers_collection = db["numbers"]

# Define admin IDs
BOSS_ID = 6917210823
ADMIN_IDS = [6917210457, 6917210899, 4779333778]
ALL_ADMIN_IDS = [BOSS_ID] + ADMIN_IDS

# Payment info
BINANCE_ID = "394717531"
EMAIL = "achaiaroot@gmail.com"

# Define pricing plans
SUBSCRIPTION_PLANS = {
    "6_hours": {"name": "6 hours", "price": "$0.50", "duration": 6},
    "1_day": {"name": "1 day", "price": "$1.2", "duration": 24},
    "7_days": {"name": "7 days", "price": "$5", "duration": 168},
    "15_days": {"name": "15 days", "price": "$10", "duration": 360}
}

# Define FSM states
class BotStates(StatesGroup):
    start = State()
    awaiting_payment = State()
    awaiting_approval = State()
    main_menu = State()
    load_credential = State()
    browse_numbers = State()

# User data class
class User:
    def __init__(self, telegram_id, username=None):
        self.telegram_id = telegram_id
        self.username = username
        self.is_active = False
        self.subscription_type = None
        self.subscription_end = None
        self.current_sid_index = 0
        
    def to_dict(self):
        return {
            "telegram_id": self.telegram_id,
            "username": self.username,
            "is_active": self.is_active,
            "subscription_type": self.subscription_type,
            "subscription_end": self.subscription_end,
            "current_sid_index": self.current_sid_index
        }
    
    @classmethod
    def from_dict(cls, data):
        user = cls(data["telegram_id"], data.get("username"))
        user.is_active = data.get("is_active", False)
        user.subscription_type = data.get("subscription_type")
        user.subscription_end = data.get("subscription_end")
        user.current_sid_index = data.get("current_sid_index", 0)
        return user

# Utility functions
async def is_admin(user_id):
    return user_id in ALL_ADMIN_IDS

async def is_boss(user_id):
    return user_id == BOSS_ID

async def get_user(telegram_id):
    user_data = users_collection.find_one({"telegram_id": telegram_id})
    if user_data:
        return User.from_dict(user_data)
    return None

async def save_user(user):
    users_collection.update_one(
        {"telegram_id": user.telegram_id},
        {"$set": user.to_dict()},
        upsert=True
    )

async def is_subscription_active(user):
    if not user.subscription_end:
        return False
    return datetime.now() < user.subscription_end

async def update_subscription(user, plan_key):
    plan = SUBSCRIPTION_PLANS[plan_key]
    user.subscription_type = plan["name"]
    user.subscription_end = datetime.now() + timedelta(hours=plan["duration"])
    user.is_active = True
    await save_user(user)

async def get_valid_credentials():
    return list(credentials_collection.find({"is_valid": True}))
    
async def save_credential(telegram_id, sid, auth_token, is_valid=True):
    credentials_collection.insert_one({
        "telegram_id": telegram_id,
        "sid": sid,
        "auth_token": auth_token,
        "is_valid": is_valid,
        "added_on": datetime.now()
    })

async def check_twilio_credential(sid, auth_token):
    try:
        client = Client(sid, auth_token)
        # Test the credentials by making a simple API call
        client.api.accounts(sid).fetch()
        return True, None
    except TwilioRestException as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)

async def get_next_sid(user):
    credentials = await get_valid_credentials()
    if not credentials:
        return None, None
    
    if user.current_sid_index >= len(credentials):
        user.current_sid_index = 0
        await save_user(user)
    
    selected_cred = credentials[user.current_sid_index]
    return selected_cred["sid"], selected_cred["auth_token"]

async def get_canada_numbers(sid, auth_token, limit=15, exclude_numbers=None):
    if not exclude_numbers:
        exclude_numbers = []
    
    try:
        client = Client(sid, auth_token)
        available_numbers = client.available_phone_numbers('CA').local.list(limit=30)
        
        # Filter out numbers that are already taken by other users
        used_numbers = set([doc["number"] for doc in numbers_collection.find()])
        
        result = []
        for number_obj in available_numbers:
            number = number_obj.phone_number
            if number not in used_numbers and number not in exclude_numbers:
                result.append(number)
                if len(result) >= limit:
                    break
                    
        return result, None
    except TwilioRestException as e:
        return [], str(e)
    except Exception as e:
        return [], str(e)

async def buy_number(sid, auth_token, phone_number, user_id):
    try:
        client = Client(sid, auth_token)
        purchased_number = client.incoming_phone_numbers.create(phone_number=phone_number)
        
        # Save the number to the database
        numbers_collection.insert_one({
            "number": phone_number,
            "twilio_sid": purchased_number.sid,
            "user_id": user_id,
            "bought_on": datetime.now()
        })
        
        # Update user's SID index for rotation
        user = await get_user(user_id)
        if user:
            user.current_sid_index = (user.current_sid_index + 1) % len(await get_valid_credentials())
            await save_user(user)
            
        return True, purchased_number.sid
    except TwilioRestException as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)

async def delete_number(twilio_sid, auth_token, number):
    try:
        number_doc = numbers_collection.find_one({"number": number})
        if not number_doc:
            return False, "Number not found in database"
        
        client = Client(twilio_sid, auth_token)
        client.incoming_phone_numbers(number_doc["twilio_sid"]).delete()
        
        # Remove from database
        numbers_collection.delete_one({"number": number})
        return True, None
    except TwilioRestException as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)

async def check_sms(sid, auth_token, number):
    try:
        client = Client(sid, auth_token)
        messages = client.messages.list(to=number, limit=5)
        
        if not messages:
            return "No messages found"
        
        # Return most recent message content
        return messages[0].body
    except TwilioRestException as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"

# Background task to check for expired subscriptions
async def check_subscriptions():
    while True:
        try:
            expired_users = users_collection.find({
                "is_active": True,
                "subscription_end": {"$lt": datetime.now()}
            })
            
            for user_data in expired_users:
                user = User.from_dict(user_data)
                user.is_active = False
                await save_user(user)
                
                try:
                    await bot.send_message(
                        user.telegram_id,
                        "⚠️ Your subscription has expired. Please purchase a new subscription to continue using the service."
                    )
                except Exception as e:
                    logging.error(f"Failed to notify user {user.telegram_id} about subscription expiry: {e}")
                    
        except Exception as e:
            logging.error(f"Error in subscription check task: {e}")
            
        await asyncio.sleep(60 * 15)  # Check every 15 minutes

# Create keyboards
def get_start_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("Start", callback_data="start"))
    return keyboard

def get_subscription_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    for plan_key, plan in SUBSCRIPTION_PLANS.items():
        button_text = f"{plan['name']} - {plan['price']}"
        keyboard.insert(InlineKeyboardButton(button_text, callback_data=f"plan_{plan_key}"))
    return keyboard

def get_admin_approval_keyboard(user_id):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("Approve", callback_data=f"approve_{user_id}"))
    keyboard.add(InlineKeyboardButton("Deny", callback_data=f"deny_{user_id}"))
    return keyboard

def get_main_menu_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔑 Load Credential", callback_data="load_credential"))
    return keyboard

def get_number_action_keyboard(number):
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("Buy", callback_data=f"buy_{number}"),
        InlineKeyboardButton("OTP", callback_data=f"otp_{number}"),
        InlineKeyboardButton("Copy", callback_data=f"copy_{number}"),
        InlineKeyboardButton("Delete", callback_data=f"delete_{number}")
    )
    keyboard.add(InlineKeyboardButton("Refresh Numbers", callback_data="refresh_numbers"))
    keyboard.add(InlineKeyboardButton("Back to Main Menu", callback_data="main_menu"))
    return keyboard

# Command handlers
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Check if user exists
    user = await get_user(user_id)
    if not user:
        user = User(user_id, username)
        await save_user(user)
    
    # Check if admin
    if await is_admin(user_id):
        await message.reply("Welcome, Admin! You have full access to the bot.")
        await BotStates.main_menu.set()
        return
    
    # Check if subscription is active
    if user.is_active and await is_subscription_active(user):
        await message.reply(
            f"Welcome back! Your subscription is active until {user.subscription_end.strftime('%Y-%m-%d %H:%M:%S')}",
            reply_markup=get_main_menu_keyboard()
        )
        await BotStates.main_menu.set()
    else:
        # Show welcome message with start button
        await message.reply(
            "Welcome to the Twilio Number Bot!\nPress the Start button below to begin.",
            reply_markup=get_start_keyboard()
        )
        await BotStates.start.set()

@dp.message_handler(commands=['admin'], state='*')
async def cmd_admin(message: types.Message):
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.reply("You don't have permission to use this command.")
        return
    
    # Admin stats
    total_users = users_collection.count_documents({})
    active_users = users_collection.count_documents({"is_active": True})
    total_credentials = credentials_collection.count_documents({})
    valid_credentials = credentials_collection.count_documents({"is_valid": True})
    total_numbers = numbers_collection.count_documents({})
    
    stats = (
        f"📊 Admin Stats:\n"
        f"Total Users: {total_users}\n"
        f"Active Users: {active_users}\n"
        f"Total Credentials: {total_credentials}\n"
        f"Valid Credentials: {valid_credentials}\n"
        f"Purchased Numbers: {total_numbers}"
    )
    
    await message.reply(stats)

@dp.message_handler(commands=['deleteuser'], state='*')
async def cmd_delete_user(message: types.Message):
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.reply("You don't have permission to use this command.")
        return
    
    # Extract the target user ID
    args = message.get_args()
    if not args:
        await message.reply("Please provide a user ID to delete. Usage: /deleteuser USER_ID")
        return
    
    try:
        target_id = int(args)
        result = users_collection.delete_one({"telegram_id": target_id})
        
        if result.deleted_count > 0:
            await message.reply(f"User {target_id} has been deleted successfully.")
        else:
            await message.reply(f"User {target_id} not found.")
    except ValueError:
        await message.reply("Invalid user ID. Please provide a valid numeric ID.")

# Callback query handlers
@dp.callback_query_handler(lambda c: c.data == 'start', state=BotStates.start)
async def process_start_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    await bot.send_message(
        callback_query.from_user.id,
        "Please select a subscription plan:",
        reply_markup=get_subscription_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data.startswith('plan_'), state=BotStates.start)
async def process_plan_selection(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    # Extract plan key
    plan_key = callback_query.data.split('_')[1]
    plan = SUBSCRIPTION_PLANS[plan_key]
    
    # Store selected plan in state
    await state.update_data(selected_plan=plan_key)
    
    payment_message = (
        f"Please send payment of {plan['price']} to:\n\n"
        f"Binance ID: {BINANCE_ID}\n"
        f"Email: {EMAIL}\n\n"
        f"After payment, please send a screenshot of your payment confirmation."
    )
    
    await bot.send_message(user_id, payment_message)
    await BotStates.awaiting_payment.set()

@dp.message_handler(content_types=['photo'], state=BotStates.awaiting_payment)
async def process_payment_screenshot(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    # Get user data
    state_data = await state.get_data()
    plan_key = state_data.get('selected_plan')
    
    if not plan_key:
        await message.reply("Something went wrong. Please try again.")
        await BotStates.start.set()
        return
    
    plan = SUBSCRIPTION_PLANS[plan_key]
    
    await message.reply("Thank you for your payment. Your subscription request has been sent to administrators for approval.")
    
    # Forward screenshot to all admins
    for admin_id in ALL_ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id,
                message.photo[-1].file_id,
                caption=(
                    f"New subscription request:\n"
                    f"User: {username} (ID: {user_id})\n"
                    f"Plan: {plan['name']} - {plan['price']}"
                ),
                reply_markup=get_admin_approval_keyboard(user_id)
            )
        except Exception as e:
            logging.error(f"Failed to send approval request to admin {admin_id}: {e}")
    
    await BotStates.awaiting_approval.set()

@dp.callback_query_handler(lambda c: c.data.startswith('approve_'), state='*')
async def process_approval(callback_query: types.CallbackQuery):
    admin_id = callback_query.from_user.id
    
    if not await is_admin(admin_id):
        await bot.answer_callback_query(callback_query.id, "You don't have permission for this action")
        return
    
    await bot.answer_callback_query(callback_query.id)
    
    # Extract user ID from callback data
    user_id = int(callback_query.data.split('_')[1])
    
    # Get user
    user = await get_user(user_id)
    if not user:
        await bot.send_message(admin_id, f"Error: User {user_id} not found")
        return
    
    # Get message to extract plan
    caption = callback_query.message.caption
    plan_line = [line for line in caption.split('\n') if line.startswith('Plan:')][0]
    plan_name = plan_line.split('- ')[0].replace('Plan:', '').strip()
    
    # Find the plan key
    plan_key = None
    for key, plan in SUBSCRIPTION_PLANS.items():
        if plan["name"] == plan_name:
            plan_key = key
            break
    
    if not plan_key:
        await bot.send_message(admin_id, f"Error: Plan not found in the approval message")
        return
    
    # Update user subscription
    await update_subscription(user, plan_key)
    
    # Notify admin
    await bot.edit_message_caption(
        chat_id=admin_id,
        message_id=callback_query.message.message_id,
        caption=f"{caption}\n\n✅ APPROVED by {callback_query.from_user.username or admin_id}"
    )
    
    # Notify user
    try:
        await bot.send_message(
            user_id,
            f"✅ Your subscription has been approved!\n"
            f"Plan: {SUBSCRIPTION_PLANS[plan_key]['name']}\n"
            f"Expires: {user.subscription_end.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"You now have access to the bot's features.",
            reply_markup=get_main_menu_keyboard()
        )
        await bot.send_message(user_id, "Please load your Twilio credentials to start using the service.")
    except Exception as e:
        error_msg = f"Failed to notify user {user_id} about approval: {e}"
        logging.error(error_msg)
        await bot.send_message(admin_id, error_msg)

@dp.callback_query_handler(lambda c: c.data.startswith('deny_'), state='*')
async def process_denial(callback_query: types.CallbackQuery):
    admin_id = callback_query.from_user.id
    
    if not await is_admin(admin_id):
        await bot.answer_callback_query(callback_query.id, "You don't have permission for this action")
        return
    
    await bot.answer_callback_query(callback_query.id)
    
    # Extract user ID from callback data
    user_id = int(callback_query.data.split('_')[1])
    
    # Update message caption
    caption = callback_query.message.caption
    await bot.edit_message_caption(
        chat_id=admin_id,
        message_id=callback_query.message.message_id,
        caption=f"{caption}\n\n❌ DENIED by {callback_query.from_user.username or admin_id}"
    )
    
    # Notify user
    try:
        await bot.send_message(
            user_id,
            "❌ Your subscription request has been denied. Please contact support for more information."
        )
    except Exception as e:
        error_msg = f"Failed to notify user {user_id} about denial: {e}"
        logging.error(error_msg)
        await bot.send_message(admin_id, error_msg)

@dp.callback_query_handler(lambda c: c.data == 'load_credential', state=BotStates.main_menu)
async def process_load_credential(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    await bot.send_message(
        callback_query.from_user.id,
        "Please send your Twilio credentials in the format: `SID AUTH_TOKEN`\n"
        "Example: `AC1234abcd... 9876zyxw...`"
    )
    await BotStates.load_credential.set()

@dp.message_handler(state=BotStates.load_credential)
async def process_credential(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Parse the SID and auth token
    try:
        cred_parts = message.text.strip().split()
        if len(cred_parts) < 2:
            await message.reply("Invalid format. Please send your credentials as: `SID AUTH_TOKEN`")
            return
        
        sid = cred_parts[0]
        auth_token = cred_parts[1]
        
        # Check if credentials are valid
        is_valid, error = await check_twilio_credential(sid, auth_token)
        
        if is_valid:
            await save_credential(user_id, sid, auth_token)
            
            await message.reply(
                "✅ Twilio credentials verified and saved successfully. You can now browse available numbers.",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("Browse Numbers", callback_data="browse_numbers")
                )
            )
            
            # Delete the message containing credentials for security
            await message.delete()
            
            await BotStates.main_menu.set()
        else:
            # Send error to user
            await message.reply(f"❌ Invalid Twilio credentials. Error: {error}")
            
            # If user is not admin, also notify admins
            if not await is_admin(user_id):
                for admin_id in ALL_ADMIN_IDS:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"⚠️ User {message.from_user.username or user_id} provided invalid credentials.\nError: {error}"
                        )
                    except Exception as e:
                        logging.error(f"Failed to notify admin {admin_id} about credential error: {e}")
            
            # Delete the message containing credentials for security
            await message.delete()
    except Exception as e:
        await message.reply(f"An error occurred: {str(e)}")
        await message.delete()  # Delete for security

@dp.callback_query_handler(lambda c: c.data == 'browse_numbers', state=BotStates.main_menu)
async def browse_numbers(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    user = await get_user(user_id)
    if not user:
        await bot.send_message(user_id, "Error: User not found")
        return
        
    # Check if subscription is active
    if not await is_admin(user_id) and (not user.is_active or not await is_subscription_active(user)):
        await bot.send_message(
            user_id,
            "Your subscription has expired. Please purchase a new subscription."
        )
        await BotStates.start.set()
        return
    
    # Get next valid SID/Auth token for this user
    sid, auth_token = await get_next_sid(user)
    
    if not sid or not auth_token:
        if await is_admin(user_id):
            await bot.send_message(user_id, "No valid Twilio credentials found. Please add credentials.")
        else:
            await bot.send_message(user_id, "Service is temporarily unavailable. Please try again later.")
            # Notify admins
            for admin_id in ALL_ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ No valid credentials available for user {callback_query.from_user.username or user_id}"
                    )
                except Exception as e:
                    logging.error(f"Failed to notify admin {admin_id}: {e}")
        return
    
    # Get Canada numbers
    loading_message = await bot.send_message(user_id, "Loading available Canada numbers...")
    
    numbers, error = await get_canada_numbers(sid, auth_token)
    
    if error or not numbers:
        error_message = f"Failed to fetch numbers: {error}" if error else "No numbers available"
        
        if await is_admin(user_id):
            await bot.edit_message_text(
                error_message,
                chat_id=user_id,
                message_id=loading_message.message_id
            )
        else:
            await bot.edit_message_text(
                "Service is temporarily unavailable. Please try again later.",
                chat_id=user_id,
                message_id=loading_message.message_id
            )
            
            # Notify admins
            for admin_id in ALL_ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ Error fetching numbers for user {callback_query.from_user.username or user_id}: {error}"
                    )
                except Exception as e:
                    logging.error(f"Failed to notify admin {admin_id}: {e}")
        return
    
    # Store numbers in state
    await state.update_data(current_numbers=numbers)
    
    # Show numbers
    numbers_text = "Available Canada Numbers:\n\n"
    for i, number in enumerate(numbers[:15], 1):
        numbers_text += f"{i}. {number}\n"
    
    await bot.edit_message_text(
        numbers_text,
        chat_id=user_id,
        message_id=loading_message.message_id,
        reply_markup=get_number_action_keyboard(numbers[0])  # Show actions for first number
    )
    
    await BotStates.browse_numbers.set()

@dp.callback_query_handler(lambda c: c.data == 'refresh_numbers', state=BotStates.browse_numbers)
async def refresh_numbers(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    user = await get_user(user_id)
    if not user:
        await bot.send_message(user_id, "Error: User not found")
        return
    
    # Get current numbers to exclude them
    state_data = await state.get_data()
    current_numbers = state_data.get("current_numbers", [])
    
    # Get next SID/Auth token
    sid, auth_token = await get_next_sid(user)
    
    if not sid or not auth_token:
        await bot.send_message(
            user_id,
            "No valid Twilio credentials available. Please try again later."
        )
        return
    
    # Get new batch of numbers
    loading_message = await bot.send_message(user_id, "Refreshing numbers...")
    
    numbers, error = await get_canada_numbers(sid, auth_token, exclude_numbers=current_numbers)
    
    if error or not numbers:
        error_message = f"Failed to fetch new numbers: {error}" if error else "No new numbers available"
        
        if await is_admin(user_id):
            await bot.edit_message_text(
                error_message,
                chat_id=user_id,
                message_id=loading_message.message_id
            )
        else:
            await bot.edit_message_text(
                "No new numbers available. Please try again later.",
                chat_id=user_id,
                message_id=loading_message.message_id
            )
        return
    
    # Update numbers in state
    await state.update_data(current_numbers=numbers)
    
    # Show new numbers
    numbers_text = "Available Canada Numbers:\n\n"
    for i, number in enumerate(numbers[:15], 1):
        numbers_text += f"{i}. {number}\n"
    
    await bot.edit_message_text(
        numbers_text,
        chat_id=user_id,
        message_id=loading_message.message_id,
        reply_markup=get_number_action_keyboard(numbers[0])  # Show actions for first number
    )

@dp.callback_query_handler(lambda c: c.data.startswith('buy_'), state=BotStates.browse_numbers)
async def buy_number_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    # Extract number from callback data
    number = callback_query.data.split('_')[1]
    
    user = await get_user(user_id)
    if not user:
        await bot.send_message(user_id, "Error: User not found")
        return
    
    # Get SID/Auth token
    sid, auth_token = await get_next_sid(user)
    
    if not sid or not auth_token:
        await bot.send_message(user_id, "No valid Twilio credentials available. Please try again later.")
        return
    
    # Process purchase
    purchasing_messageimport os
import logging
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from dotenv import load_dotenv
import pymongo

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)

# Initialize bot and dispatcher
bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# MongoDB connection
mongo_client = pymongo.MongoClient(os.getenv("MONGODB_URI"))
db = mongo_client["telegram_twilio_bot"]
users_collection = db["users"]
credentials_collection = db["credentials"]
numbers_collection = db["numbers"]

# Define admin IDs
BOSS_ID = 6917210823
ADMIN_IDS = [6917210457, 6917210899, 4779333778]
ALL_ADMIN_IDS = [BOSS_ID] + ADMIN_IDS

# Payment info
BINANCE_ID = "394717531"
EMAIL = "achaiaroot@gmail.com"

# Define pricing plans
SUBSCRIPTION_PLANS = {
    "6_hours": {"name": "6 hours", "price": "$0.50", "duration": 6},
    "1_day": {"name": "1 day", "price": "$1.2", "duration": 24},
    "7_days": {"name": "7 days", "price": "$5", "duration": 168},
    "15_days": {"name": "15 days", "price": "$10", "duration": 360}
}

# Define FSM states
class BotStates(StatesGroup):
    start = State()
    awaiting_payment = State()
    awaiting_approval = State()
    main_menu = State()
    load_credential = State()
    browse_numbers = State()

# User data class
class User:
    def __init__(self, telegram_id, username=None):
        self.telegram_id = telegram_id
        self.username = username
        self.is_active = False
        self.subscription_type = None
        self.subscription_end = None
        self.current_sid_index = 0
        
    def to_dict(self):
        return {
            "telegram_id": self.telegram_id,
            "username": self.username,
            "is_active": self.is_active,
            "subscription_type": self.subscription_type,
            "subscription_end": self.subscription_end,
            "current_sid_index": self.current_sid_index
        }
    
    @classmethod
    def from_dict(cls, data):
        user = cls(data["telegram_id"], data.get("username"))
        user.is_active = data.get("is_active", False)
        user.subscription_type = data.get("subscription_type")
        user.subscription_end = data.get("subscription_end")
        user.current_sid_index = data.get("current_sid_index", 0)
        return user

# Utility functions
async def is_admin(user_id):
    return user_id in ALL_ADMIN_IDS

async def is_boss(user_id):
    return user_id == BOSS_ID

async def get_user(telegram_id):
    user_data = users_collection.find_one({"telegram_id": telegram_id})
    if user_data:
        return User.from_dict(user_data)
    return None

async def save_user(user):
    users_collection.update_one(
        {"telegram_id": user.telegram_id},
        {"$set": user.to_dict()},
        upsert=True
    )

async def is_subscription_active(user):
    if not user.subscription_end:
        return False
    return datetime.now() < user.subscription_end

async def update_subscription(user, plan_key):
    plan = SUBSCRIPTION_PLANS[plan_key]
    user.subscription_type = plan["name"]
    user.subscription_end = datetime.now() + timedelta(hours=plan["duration"])
    user.is_active = True
    await save_user(user)

async def get_valid_credentials():
    return list(credentials_collection.find({"is_valid": True}))
    
async def save_credential(telegram_id, sid, auth_token, is_valid=True):
    credentials_collection.insert_one({
        "telegram_id": telegram_id,
        "sid": sid,
        "auth_token": auth_token,
        "is_valid": is_valid,
        "added_on": datetime.now()
    })

async def check_twilio_credential(sid, auth_token):
    try:
        client = Client(sid, auth_token)
        # Test the credentials by making a simple API call
        client.api.accounts(sid).fetch()
        return True, None
    except TwilioRestException as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)

async def get_next_sid(user):
    credentials = await get_valid_credentials()
    if not credentials:
        return None, None
    
    if user.current_sid_index >= len(credentials):
        user.current_sid_index = 0
        await save_user(user)
    
    selected_cred = credentials[user.current_sid_index]
    return selected_cred["sid"], selected_cred["auth_token"]

async def get_canada_numbers(sid, auth_token, limit=15, exclude_numbers=None):
    if not exclude_numbers:
        exclude_numbers = []
    
    try:
        client = Client(sid, auth_token)
        available_numbers = client.available_phone_numbers('CA').local.list(limit=30)
        
        # Filter out numbers that are already taken by other users
        used_numbers = set([doc["number"] for doc in numbers_collection.find()])
        
        result = []
        for number_obj in available_numbers:
            number = number_obj.phone_number
            if number not in used_numbers and number not in exclude_numbers:
                result.append(number)
                if len(result) >= limit:
                    break
                    
        return result, None
    except TwilioRestException as e:
        return [], str(e)
    except Exception as e:
        return [], str(e)

async def buy_number(sid, auth_token, phone_number, user_id):
    try:
        client = Client(sid, auth_token)
        purchased_number = client.incoming_phone_numbers.create(phone_number=phone_number)
        
        # Save the number to the database
        numbers_collection.insert_one({
            "number": phone_number,
            "twilio_sid": purchased_number.sid,
            "user_id": user_id,
            "bought_on": datetime.now()
        })
        
        # Update user's SID index for rotation
        user = await get_user(user_id)
        if user:
            user.current_sid_index = (user.current_sid_index + 1) % len(await get_valid_credentials())
            await save_user(user)
            
        return True, purchased_number.sid
    except TwilioRestException as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)

async def delete_number(twilio_sid, auth_token, number):
    try:
        number_doc = numbers_collection.find_one({"number": number})
        if not number_doc:
            return False, "Number not found in database"
        
        client = Client(twilio_sid, auth_token)
        client.incoming_phone_numbers(number_doc["twilio_sid"]).delete()
        
        # Remove from database
        numbers_collection.delete_one({"number": number})
        return True, None
    except TwilioRestException as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)

async def check_sms(sid, auth_token, number):
    try:
        client = Client(sid, auth_token)
        messages = client.messages.list(to=number, limit=5)
        
        if not messages:
            return "No messages found"
        
        # Return most recent message content
        return messages[0].body
    except TwilioRestException as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"

# Background task to check for expired subscriptions
async def check_subscriptions():
    while True:
        try:
            expired_users = users_collection.find({
                "is_active": True,
                "subscription_end": {"$lt": datetime.now()}
            })
            
            for user_data in expired_users:
                user = User.from_dict(user_data)
                user.is_active = False
                await save_user(user)
                
                try:
                    await bot.send_message(
                        user.telegram_id,
                        "⚠️ Your subscription has expired. Please purchase a new subscription to continue using the service."
                    )
                except Exception as e:
                    logging.error(f"Failed to notify user {user.telegram_id} about subscription expiry: {e}")
                    
        except Exception as e:
            logging.error(f"Error in subscription check task: {e}")
            
        await asyncio.sleep(60 * 15)  # Check every 15 minutes

# Create keyboards
def get_start_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("Start", callback_data="start"))
    return keyboard

def get_subscription_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    for plan_key, plan in SUBSCRIPTION_PLANS.items():
        button_text = f"{plan['name']} - {plan['price']}"
        keyboard.insert(InlineKeyboardButton(button_text, callback_data=f"plan_{plan_key}"))
    return keyboard

def get_admin_approval_keyboard(user_id):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("Approve", callback_data=f"approve_{user_id}"))
    keyboard.add(InlineKeyboardButton("Deny", callback_data=f"deny_{user_id}"))
    return keyboard

def get_main_menu_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔑 Load Credential", callback_data="load_credential"))
    return keyboard

def get_number_action_keyboard(number):
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("Buy", callback_data=f"buy_{number}"),
        InlineKeyboardButton("OTP", callback_data=f"otp_{number}"),
        InlineKeyboardButton("Copy", callback_data=f"copy_{number}"),
        InlineKeyboardButton("Delete", callback_data=f"delete_{number}")
    )
    keyboard.add(InlineKeyboardButton("Refresh Numbers", callback_data="refresh_numbers"))
    keyboard.add(InlineKeyboardButton("Back to Main Menu", callback_data="main_menu"))
    return keyboard

# Command handlers
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Check if user exists
    user = await get_user(user_id)
    if not user:
        user = User(user_id, username)
        await save_user(user)
    
    # Check if admin
    if await is_admin(user_id):
        await message.reply("Welcome, Admin! You have full access to the bot.")
        await BotStates.main_menu.set()
        return
    
    # Check if subscription is active
    if user.is_active and await is_subscription_active(user):
        await message.reply(
            f"Welcome back! Your subscription is active until {user.subscription_end.strftime('%Y-%m-%d %H:%M:%S')}",
            reply_markup=get_main_menu_keyboard()
        )
        await BotStates.main_menu.set()
    else:
        # Show welcome message with start button
        await message.reply(
            "Welcome to the Twilio Number Bot!\nPress the Start button below to begin.",
            reply_markup=get_start_keyboard()
        )
        await BotStates.start.set()

@dp.message_handler(commands=['admin'], state='*')
async def cmd_admin(message: types.Message):
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.reply("You don't have permission to use this command.")
        return
    
    # Admin stats
    total_users = users_collection.count_documents({})
    active_users = users_collection.count_documents({"is_active": True})
    total_credentials = credentials_collection.count_documents({})
    valid_credentials = credentials_collection.count_documents({"is_valid": True})
    total_numbers = numbers_collection.count_documents({})
    
    stats = (
        f"📊 Admin Stats:\n"
        f"Total Users: {total_users}\n"
        f"Active Users: {active_users}\n"
        f"Total Credentials: {total_credentials}\n"
        f"Valid Credentials: {valid_credentials}\n"
        f"Purchased Numbers: {total_numbers}"
    )
    
    await message.reply(stats)

@dp.message_handler(commands=['deleteuser'], state='*')
async def cmd_delete_user(message: types.Message):
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.reply("You don't have permission to use this command.")
        return
    
    # Extract the target user ID
    args = message.get_args()
    if not args:
        await message.reply("Please provide a user ID to delete. Usage: /deleteuser USER_ID")
        return
    
    try:
        target_id = int(args)
        result = users_collection.delete_one({"telegram_id": target_id})
        
        if result.deleted_count > 0:
            await message.reply(f"User {target_id} has been deleted successfully.")
        else:
            await message.reply(f"User {target_id} not found.")
    except ValueError:
        await message.reply("Invalid user ID. Please provide a valid numeric ID.")

# Callback query handlers
@dp.callback_query_handler(lambda c: c.data == 'start', state=BotStates.start)
async def process_start_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    await bot.send_message(
        callback_query.from_user.id,
        "Please select a subscription plan:",
        reply_markup=get_subscription_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data.startswith('plan_'), state=BotStates.start)
async def process_plan_selection(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    # Extract plan key
    plan_key = callback_query.data.split('_')[1]
    plan = SUBSCRIPTION_PLANS[plan_key]
    
    # Store selected plan in state
    await state.update_data(selected_plan=plan_key)
    
    payment_message = (
        f"Please send payment of {plan['price']} to:\n\n"
        f"Binance ID: {BINANCE_ID}\n"
        f"Email: {EMAIL}\n\n"
        f"After payment, please send a screenshot of your payment confirmation."
    )
    
    await bot.send_message(user_id, payment_message)
    await BotStates.awaiting_payment.set()

@dp.message_handler(content_types=['photo'], state=BotStates.awaiting_payment)
async def process_payment_screenshot(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    # Get user data
    state_data = await state.get_data()
    plan_key = state_data.get('selected_plan')
    
    if not plan_key:
        await message.reply("Something went wrong. Please try again.")
        await BotStates.start.set()
        return
    
    plan = SUBSCRIPTION_PLANS[plan_key]
    
    await message.reply("Thank you for your payment. Your subscription request has been sent to administrators for approval.")
    
    # Forward screenshot to all admins
    for admin_id in ALL_ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id,
                message.photo[-1].file_id,
                caption=(
                    f"New subscription request:\n"
                    f"User: {username} (ID: {user_id})\n"
                    f"Plan: {plan['name']} - {plan['price']}"
                ),
                reply_markup=get_admin_approval_keyboard(user_id)
            )
        except Exception as e:
            logging.error(f"Failed to send approval request to admin {admin_id}: {e}")
    
    await BotStates.awaiting_approval.set()

@dp.callback_query_handler(lambda c: c.data.startswith('approve_'), state='*')
async def process_approval(callback_query: types.CallbackQuery):
    admin_id = callback_query.from_user.id
    
    if not await is_admin(admin_id):
        await bot.answer_callback_query(callback_query.id, "You don't have permission for this action")
        return
    
    await bot.answer_callback_query(callback_query.id)
    
    # Extract user ID from callback data
    user_id = int(callback_query.data.split('_')[1])
    
    # Get user
    user = await get_user(user_id)
    if not user:
        await bot.send_message(admin_id, f"Error: User {user_id} not found")
        return
    
    # Get message to extract plan
    caption = callback_query.message.caption
    plan_line = [line for line in caption.split('\n') if line.startswith('Plan:')][0]
    plan_name = plan_line.split('- ')[0].replace('Plan:', '').strip()
    
    # Find the plan key
    plan_key = None
    for key, plan in SUBSCRIPTION_PLANS.items():
        if plan["name"] == plan_name:
            plan_key = key
            break
    
    if not plan_key:
        await bot.send_message(admin_id, f"Error: Plan not found in the approval message")
        return
    
    # Update user subscription
    await update_subscription(user, plan_key)
    
    # Notify admin
    await bot.edit_message_caption(
        chat_id=admin_id,
        message_id=callback_query.message.message_id,
        caption=f"{caption}\n\n✅ APPROVED by {callback_query.from_user.username or admin_id}"
    )
    
    # Notify user
    try:
        await bot.send_message(
            user_id,
            f"✅ Your subscription has been approved!\n"
            f"Plan: {SUBSCRIPTION_PLANS[plan_key]['name']}\n"
            f"Expires: {user.subscription_end.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"You now have access to the bot's features.",
            reply_markup=get_main_menu_keyboard()
        )
        await bot.send_message(user_id, "Please load your Twilio credentials to start using the service.")
    except Exception as e:
        error_msg = f"Failed to notify user {user_id} about approval: {e}"
        logging.error(error_msg)
        await bot.send_message(admin_id, error_msg)

@dp.callback_query_handler(lambda c: c.data.startswith('deny_'), state='*')
async def process_denial(callback_query: types.CallbackQuery):
    admin_id = callback_query.from_user.id
    
    if not await is_admin(admin_id):
        await bot.answer_callback_query(callback_query.id, "You don't have permission for this action")
        return
    
    await bot.answer_callback_query(callback_query.id)
    
    # Extract user ID from callback data
    user_id = int(callback_query.data.split('_')[1])
    
    # Update message caption
    caption = callback_query.message.caption
    await bot.edit_message_caption(
        chat_id=admin_id,
        message_id=callback_query.message.message_id,
        caption=f"{caption}\n\n❌ DENIED by {callback_query.from_user.username or admin_id}"
    )
    
    # Notify user
    try:
        await bot.send_message(
            user_id,
            "❌ Your subscription request has been denied. Please contact support for more information."
        )
    except Exception as e:
        error_msg = f"Failed to notify user {user_id} about denial: {e}"
        logging.error(error_msg)
        await bot.send_message(admin_id, error_msg)

@dp.callback_query_handler(lambda c: c.data == 'load_credential', state=BotStates.main_menu)
async def process_load_credential(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    
    await bot.send_message(
        callback_query.from_user.id,
        "Please send your Twilio credentials in the format: `SID AUTH_TOKEN`\n"
        "Example: `AC1234abcd... 9876zyxw...`"
    )
    await BotStates.load_credential.set()

@dp.message_handler(state=BotStates.load_credential)
async def process_credential(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Parse the SID and auth token
    try:
        cred_parts = message.text.strip().split()
        if len(cred_parts) < 2:
            await message.reply("Invalid format. Please send your credentials as: `SID AUTH_TOKEN`")
            return
        
        sid = cred_parts[0]
        auth_token = cred_parts[1]
        
        # Check if credentials are valid
        is_valid, error = await check_twilio_credential(sid, auth_token)
        
        if is_valid:
            await save_credential(user_id, sid, auth_token)
            
            await message.reply(
                "✅ Twilio credentials verified and saved successfully. You can now browse available numbers.",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("Browse Numbers", callback_data="browse_numbers")
                )
            )
            
            # Delete the message containing credentials for security
            await message.delete()
            
            await BotStates.main_menu.set()
        else:
            # Send error to user
            await message.reply(f"❌ Invalid Twilio credentials. Error: {error}")
            
            # If user is not admin, also notify admins
            if not await is_admin(user_id):
                for admin_id in ALL_ADMIN_IDS:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"⚠️ User {message.from_user.username or user_id} provided invalid credentials.\nError: {error}"
                        )
                    except Exception as e:
                        logging.error(f"Failed to notify admin {admin_id} about credential error: {e}")
            
            # Delete the message containing credentials for security
            await message.delete()
    except Exception as e:
        await message.reply(f"An error occurred: {str(e)}")
        await message.delete()  # Delete for security

@dp.callback_query_handler(lambda c: c.data == 'browse_numbers', state=BotStates.main_menu)
async def browse_numbers(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    user = await get_user(user_id)
    if not user:
        await bot.send_message(user_id, "Error: User not found")
        return
        
    # Check if subscription is active
    if not await is_admin(user_id) and (not user.is_active or not await is_subscription_active(user)):
        await bot.send_message(
            user_id,
            "Your subscription has expired. Please purchase a new subscription."
        )
        await BotStates.start.set()
        return
    
    # Get next valid SID/Auth token for this user
    sid, auth_token = await get_next_sid(user)
    
    if not sid or not auth_token:
        if await is_admin(user_id):
            await bot.send_message(user_id, "No valid Twilio credentials found. Please add credentials.")
        else:
            await bot.send_message(user_id, "Service is temporarily unavailable. Please try again later.")
            # Notify admins
            for admin_id in ALL_ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ No valid credentials available for user {callback_query.from_user.username or user_id}"
                    )
                except Exception as e:
                    logging.error(f"Failed to notify admin {admin_id}: {e}")
        return
    
    # Get Canada numbers
    loading_message = await bot.send_message(user_id, "Loading available Canada numbers...")
    
    numbers, error = await get_canada_numbers(sid, auth_token)
    
    if error or not numbers:
        error_message = f"Failed to fetch numbers: {error}" if error else "No numbers available"
        
        if await is_admin(user_id):
            await bot.edit_message_text(
                error_message,
                chat_id=user_id,
                message_id=loading_message.message_id
            )
        else:
            await bot.edit_message_text(
                "Service is temporarily unavailable. Please try again later.",
                chat_id=user_id,
                message_id=loading_message.message_id
            )
            
            # Notify admins
            for admin_id in ALL_ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ Error fetching numbers for user {callback_query.from_user.username or user_id}: {error}"
                    )
                except Exception as e:
                    logging.error(f"Failed to notify admin {admin_id}: {e}")
        return
    
    # Store numbers in state
    await state.update_data(current_numbers=numbers)
    
    # Show numbers
    numbers_text = "Available Canada Numbers:\n\n"
    for i, number in enumerate(numbers[:15], 1):
        numbers_text += f"{i}. {number}\n"
    
    await bot.edit_message_text(
        numbers_text,
        chat_id=user_id,
        message_id=loading_message.message_id,
        reply_markup=get_number_action_keyboard(numbers[0])  # Show actions for first number
    )
    
    await BotStates.browse_numbers.set()

@dp.callback_query_handler(lambda c: c.data == 'refresh_numbers', state=BotStates.browse_numbers)
async def refresh_numbers(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    user = await get_user(user_id)
    if not user:
        await bot.send_message(user_id, "Error: User not found")
        return
    
    # Get current numbers to exclude them
    state_data = await state.get_data()
    current_numbers = state_data.get("current_numbers", [])
    
    # Get next SID/Auth token
    sid, auth_token = await get_next_sid(user)
    
    if not sid or not auth_token:
        await bot.send_message(
            user_id,
            "No valid Twilio credentials available. Please try again later."
        )
        return
    
    # Get new batch of numbers
    loading_message = await bot.send_message(user_id, "Refreshing numbers...")
    
    numbers, error = await get_canada_numbers(sid, auth_token, exclude_numbers=current_numbers)
    
    if error or not numbers:
        error_message = f"Failed to fetch new numbers: {error}" if error else "No new numbers available"
        
        if await is_admin(user_id):
            await bot.edit_message_text(
                error_message,
                chat_id=user_id,
                message_id=loading_message.message_id
            )
        else:
            await bot.edit_message_text(
                "No new numbers available. Please try again later.",
                chat_id=user_id,
                message_id=loading_message.message_id
            )
        return
    
    # Update numbers in state
    await state.update_data(current_numbers=numbers)
    
    # Show new numbers
    numbers_text = "Available Canada Numbers:\n\n"
    for i, number in enumerate(numbers[:15], 1):
        numbers_text += f"{i}. {number}\n"
    
    await bot.edit_message_text(
        numbers_text,
        chat_id=user_id,
        message_id=loading_message.message_id,
        reply_markup=get_number_action_keyboard(numbers[0])  # Show actions for first number
    )

@dp.callback_query_handler(lambda c: c.data.startswith('buy_'), state=BotStates.browse_numbers)
async def buy_number_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    # Extract number from callback data
    number = callback_query.data.split('_')[1]
    
    user = await get_user(user_id)
    if not user:
        await bot.send_message(user_id, "Error: User not found")
        return
    
    # Get SID/Auth token
    sid, auth_token = await get_next_sid(user)
    
    if not sid or not auth_token:
        await bot.send_message(user_id, "No valid Twilio credentials available. Please try again later.")
        return
    
    # Process purchase
    purchasing_message = await bot.send_message(user_id, f"Purchasing number {number}...")
    
    success, result = await buy_number(sid, auth_token, number, user_id)
    
    if success:
        await bot.edit_message_text(
            f"✅ Successfully purchased number {number}\nTwilio SID: {result}\n\n"
            "You can now receive SMS on this number.",
            chat_id=user_id,
            message_id=purchasing_message.message_id
        )
        
        # Refresh numbers list
        await browse_numbers(callback_query, state)
    else:
        if await is_admin(user_id):
            await bot.edit_message_text(
                f"❌ Failed to purchase number: {result}",
                chat_id=user_id,
                message_id=purchasing_message.message_id
            )
        else:
            await bot.edit_message_text(
                "❌ Failed to purchase number. Please try another one.",
                chat_id=user_id,
                message_id=purchasing_message.message_id
            )
            
            # Notify admins about error
            for admin_id in ALL_ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ Error buying number for user {callback_query.from_user.username or user_id}:\n"
                        f"Number: {number}\nError: {result}"
                    )
                except Exception as e:
                    logging.error(f"Failed to notify admin {admin_id}: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('otp_'), state=BotStates.browse_numbers)
async def check_otp_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    # Extract number from callback data
    number = callback_query.data.split('_')[1]
    
    user = await get_user(user_id)
    if not user:
        await bot.send_message(user_id, "Error: User not found")
        return
    
    # Get SID/Auth token
    sid, auth_token = await get_next_sid(user)
    
    if not sid or not auth_token:
        await bot.send_message(user_id, "No valid Twilio credentials available. Please try again later.")
        return
    
    # Check if the number is actually purchased
    number_doc = numbers_collection.find_one({"number": number})
    if not number_doc:
        await bot.send_message(user_id, "This number hasn't been purchased yet. Please buy it first.")
        return
    
    # Check for OTP messages
    checking_message = await bot.send_message(user_id, f"Checking for messages on {number}...")
    
    sms_content = await check_sms(sid, auth_token, number)
    
    await bot.edit_message_text(
        f"📱 Number: {number}\n\n"
        f"📨 Latest SMS:\n{sms_content}",
        chat_id=user_id,
        message_id=checking_message.message_id,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("Refresh", callback_data=f"otp_{number}"),
            InlineKeyboardButton("Back", callback_data="browse_numbers")
        )
    )

@dp.callback_query_handler(lambda c: c.data.startswith('copy_'), state=BotStates.browse_numbers)
async def copy_number_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id, text="Number copied to clipboard")
    
    # Extract number from callback data
    number = callback_query.data.split('_')[1]
    
    # Send as a separate message for easy copying
    await bot.send_message(
        callback_query.from_user.id,
        f"`{number}`",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query_handler(lambda c: c.data.startswith('delete_'), state=BotStates.browse_numbers)
async def delete_number_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    # Extract number from callback data
    number = callback_query.data.split('_')[1]
    
    # Check if the number belongs to this user
    number_doc = numbers_collection.find_one({"number": number})
    if not number_doc:
        await bot.send_message(user_id, "This number doesn't exist or hasn't been purchased.")
        return
    
    if number_doc["user_id"] != user_id and not await is_admin(user_id):
        await bot.send_message(user_id, "You don't have permission to delete this number.")
        return
    
    user = await get_user(user_id)
    if not user:
        await bot.send_message(user_id, "Error: User not found")
        return
    
    # Get SID/Auth token
    sid, auth_token = await get_next_sid(user)
    
    if not sid or not auth_token:
        await bot.send_message(user_id, "No valid Twilio credentials available. Please try again later.")
        return
    
    # Delete the number
    deleting_message = await bot.send_message(user_id, f"Deleting number {number}...")
    
    success, error = await delete_number(sid, auth_token, number)
    
    if success:
        await bot.edit_message_text(
            f"✅ Successfully deleted number {number}",
            chat_id=user_id,
            message_id=deleting_message.message_id
        )
        
        # Refresh numbers list
        await browse_numbers(callback_query, state)
    else:
        if await is_admin(user_id):
            await bot.edit_message_text(
                f"❌ Failed to delete number: {error}",
                chat_id=user_id,
                message_id=deleting_message.message_id
            )
        else:
            await bot.edit_message_text(
                "❌ Failed to delete number. Please try again later.",
                chat_id=user_id,
                message_id=deleting_message.message_id
            )
            
            # Notify admins about error
            for admin_id in ALL_ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ Error deleting number for user {callback_query.from_user.username or user_id}:\n"
                        f"Number: {number}\nError: {error}"
                    )
                except Exception as e:
                    logging.error(f"Failed to notify admin {admin_id}: {e}")

@dp.callback_query_handler(lambda c: c.data == 'main_menu', state='*')
async def back_to_main_menu(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    
    await bot.send_message(
        user_id,
        "Main Menu",
        reply_markup=get_main_menu_keyboard()
    )
    
    await BotStates.main_menu.set()

# Error handler
@dp.errors_handler()
async def error_handler(update, exception):
    # Log the error
    logging.error(f"Update {update} caused error {exception}")
    
    try:
        # Get user ID from update
        if update.message:
            user_id = update.message.from_user.id
        elif update.callback_query:
            user_id = update.callback_query.from_user.id
        else:
            return True
            
        # Notify user
        await bot.send_message(
            user_id,
            "An error occurred while processing your request. Please try again later."
        )
        
        # Notify admins if not an admin
        if not await is_admin(user_id):
            error_message = f"Error for user {user_id}: {str(exception)}"
            for admin_id in ALL_ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, f"⚠️ {error_message}")
                except Exception as e:
                    logging.error(f"Failed to notify admin {admin_id} about error: {e}")
    except Exception as e:
        logging.error(f"Error in error handler: {e}")
    
    return True

async def on_startup(dp):
    # Start background task
    asyncio.create_task(check_subscriptions())

    # Set command list
    commands = [
        types.BotCommand(command="start", description="Start the bot"),
        types.BotCommand(command="admin", description="Admin statistics (admin only)"),
        types.BotCommand(command="deleteuser", description="Delete a user (admin only)")
    ]
    await bot.set_my_commands(commands)

async def on_shutdown(dp):
    await bot.delete_my_commands()

if __name__ == '__main__':
    from aiogram import executor
    
    # Start the bot
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown)