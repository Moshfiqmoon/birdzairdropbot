import os
import random
import asyncio
import sqlite3
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Union
import discord
from discord.ext import commands as discord_commands
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from web3 import Web3, Account
import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.system_program import TransferParams, transfer
from solders.message import Message
from openpyxl import Workbook
from dotenv import load_dotenv
from ratelimit import limits, sleep_and_retry
import json
import hashlib
import pytz
from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.transactions import Payment
from xrpl.utils import xrp_to_drops

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ETH_RPC_URL = os.getenv('ETH_RPC_URL', 'https://mainnet.infura.io/v3/your-infura-key')
BSC_RPC_URL = os.getenv('BSC_RPC_URL', 'https://bsc-dataseed.binance.org/')
SOL_RPC_URL = os.getenv('SOL_RPC_URL', 'https://api.devnet.solana.com')
XRP_RPC_URL = os.getenv('XRP_RPC_URL', 'https://s1.ripple.com:51234/')
ADMIN_ID = os.getenv('ADMIN_ID')
ETH_SENDER_ADDRESS = os.getenv('ETH_SENDER_ADDRESS')
ETH_PRIVATE_KEY = os.getenv('ETH_PRIVATE_KEY')
SOL_SENDER_PRIVATE_KEY = os.getenv('SOL_SENDER_PRIVATE_KEY')
XRP_SENDER_ADDRESS = os.getenv('XRP_SENDER_ADDRESS')
XRP_SENDER_SEED = os.getenv('XRP_SENDER_SEED')
TOKEN_CONTRACT_ADDRESS = os.getenv('TOKEN_CONTRACT_ADDRESS')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'tigerr_airdrop_bot')

# Blockchain Setup
web3_eth = Web3(Web3.HTTPProvider(ETH_RPC_URL))
web3_bsc = Web3(Web3.HTTPProvider(BSC_RPC_URL))
solana_client = requests.Session()
solana_client.headers.update({"Content-Type": "application/json"})
xrp_client = JsonRpcClient(XRP_RPC_URL)

# ERC-20 Token ABI
TOKEN_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}
]

# Logging Setup
logging.basicConfig(filename='airdrop_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# SQLite Setup
conn = sqlite3.connect('airdrop.db', check_same_thread=False)
cursor = conn.cursor()

cursor.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY, username TEXT, language TEXT, referral_code TEXT, referred_by TEXT,
        kyc_status TEXT DEFAULT 'pending', agreed_terms INTEGER, Birdz_balance REAL DEFAULT 0,
        kyc_telegram_link TEXT, kyc_x_link TEXT, kyc_wallet TEXT, kyc_chain TEXT, kyc_submission_time TEXT,
        has_seen_menu INTEGER DEFAULT 0, joined_groups INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS captchas (user_id TEXT PRIMARY KEY, captcha INTEGER, timestamp TEXT);
    CREATE TABLE IF NOT EXISTS submissions (user_id TEXT PRIMARY KEY, wallet TEXT, chain TEXT, timestamp TEXT);
    CREATE TABLE IF NOT EXISTS eligible (user_id TEXT PRIMARY KEY, wallet TEXT, chain TEXT, tier INTEGER, verified INTEGER, token_balance REAL, social_tasks_completed INTEGER);
    CREATE TABLE IF NOT EXISTS distributions (user_id TEXT PRIMARY KEY, wallet TEXT, chain TEXT, amount REAL, status TEXT, tx_hash TEXT);
    CREATE TABLE IF NOT EXISTS referrals (referrer_id TEXT, referee_id TEXT PRIMARY KEY, timestamp TEXT, status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS blacklist (wallet TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS whitelist (wallet TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, start_date TEXT, end_date TEXT, total_tokens REAL, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS daily_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT, reward REAL DEFAULT 10, active INTEGER DEFAULT 1, mandatory INTEGER DEFAULT 0, task_link TEXT);
    CREATE TABLE IF NOT EXISTS task_completions (user_id TEXT, task_id INTEGER, completion_date TEXT, username TEXT, status TEXT DEFAULT 'pending', PRIMARY KEY (user_id, task_id, completion_date));
    CREATE TABLE IF NOT EXISTS admin_states (
        user_id TEXT PRIMARY KEY,
        state TEXT,
        task_id TEXT,
        timestamp TEXT
    );
    CREATE TABLE IF NOT EXISTS tokens (
        token_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        contract_address TEXT,
        chain TEXT,
        decimals INTEGER DEFAULT 18
    );
    CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY,
        username TEXT,
        role TEXT DEFAULT 'admin',
        added_by TEXT,
        added_at TEXT,
        permissions TEXT DEFAULT 'all'
    );
    CREATE TABLE IF NOT EXISTS token_distributions (
        token_id INTEGER,
        tier INTEGER,
        amount REAL,
        contract_address TEXT,  -- Added contract_address for tier-specific tokens
        PRIMARY KEY (token_id, tier)
    );
''')
conn.commit()

# Config Initialization
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("total_supply", "1000000"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("tier_1_amount", "1000"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("tier_2_amount", "2000"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("tier_3_amount", "5000"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("referral_bonus", "15"))
cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("min_token_balance", "100"))

# Add default admin
cursor.execute("INSERT OR IGNORE INTO admins (user_id, username, role, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
               (ADMIN_ID, "Super Admin", "super_admin", "system", datetime.utcnow().isoformat()))
conn.commit()

# Sample Tokens with Tier-Specific Contract Addresses
cursor.execute("INSERT OR IGNORE INTO tokens (name, contract_address, chain) VALUES (?, ?, ?)",
               ("BirdzCoin", TOKEN_CONTRACT_ADDRESS, "ETH"))
cursor.execute("INSERT OR IGNORE INTO tokens (name, contract_address, chain) VALUES (?, ?, ?)",
               ("SampleToken", "0xAnotherTokenAddress", "BSC"))
conn.commit()

# Token distribution defaults with tier-specific contract addresses
cursor.execute("INSERT OR IGNORE INTO token_distributions (token_id, tier, amount, contract_address) VALUES (?, ?, ?, ?)",
               (1, 1, 1000, "0xTier1ETHContractAddress"))  # ETH Tier 1
cursor.execute("INSERT OR IGNORE INTO token_distributions (token_id, tier, amount, contract_address) VALUES (?, ?, ?, ?)",
               (1, 2, 2000, "0xTier2ETHContractAddress"))  # ETH Tier 2
cursor.execute("INSERT OR IGNORE INTO token_distributions (token_id, tier, amount, contract_address) VALUES (?, ?, ?, ?)",
               (1, 3, 5000, "0xTier3ETHContractAddress"))  # ETH Tier 3
cursor.execute("INSERT OR IGNORE INTO token_distributions (token_id, tier, amount, contract_address) VALUES (?, ?, ?, ?)",
               (2, 1, 1000, "0xTier1BSCContractAddress"))  # BSC Tier 1 (example)
conn.commit()

# Sample Campaign and Tasks
cursor.execute("INSERT OR IGNORE INTO campaigns (name, start_date, end_date, total_tokens, active) VALUES (?, ?, ?, ?, ?)",
               ("Launch Airdrop", datetime.utcnow().isoformat(), (datetime.utcnow() + timedelta(days=7)).isoformat(), 1000000, 1))
cursor.executescript("DELETE FROM daily_tasks")
daily_tasks = [
    ("Watch YouTube Video", 10, 0, "https://youtube.com/example"),
    ("Join Telegram", 10, 1, "https://t.me/examplegroup"),
    ("Follow Twitter", 10, 0, "https://twitter.com/example")
]
for description, reward, mandatory, task_link in daily_tasks:
    cursor.execute("INSERT OR IGNORE INTO daily_tasks (description, reward, mandatory, task_link, active) VALUES (?, ?, ?, ?, 1)",
                   (description, reward, mandatory, task_link))
conn.commit()

# Language Support (unchanged, included for completeness)
LANGUAGES = {
    "en": {
        "welcome": "🌟 Welcome to the BirdzAirdrop Bot! 🌟\n\nBalance: {balance} Birdz Coins\nReferral Link: {ref_link}",
        "mandatory_rules": "📢 Mandatory Airdrop Rules:\n\n🔹 Join @BirdzMedia\n🔹 Join @K1dandWaltLounge\n\nMust Complete All Tasks & Click On [Continue] To Proceed",
        "confirm_groups": "Please confirm you have joined both groups by clicking below:",
        "menu": "Choose an action:",
        "terms": "Terms & Conditions:\n- Participate fairly\n- No multiple accounts",
        "usage": "Select chain (ETH, BSC, SOL, XRP) and enter wallet:",
        "captcha": "Solve: {captcha} + 5 = ?",
        "verified": "Wallet verified! Tier {tier}.",
        "blacklisted": "This wallet is blacklisted.",
        "invalid_address": "Invalid {chain} address (e.g., ETH: 0x..., SOL: SoL..., XRP: r...).",
        "no_assets": "No qualifying assets found.",
        "already_submitted": "Wallet already submitted.",
        "admin_only": "Admin only.",
        "sent_tokens": "Sent {amount} tokens to {wallet} (Tx: {tx_hash})",
        "failed_tokens": "Failed to send {amount} tokens to {wallet}: {error}",
        "referral_bonus": "🎉 Congratulations! You've earned a {bonus} Birdz Coin bonus for referring {referee}!",
        "referral_pending": "Referral submitted for {referee}. Awaiting admin approval.",
        "referral_duplicate": "This user has already been referred or is a duplicate.",
        "referral_notification": "New referral submission:\nReferrer ID: {referrer_id}\nReferee ID: {referee_id}\nReferee Username: {referee_name}\nTime: {time}",
        "kyc_pending": "KYC verification pending.",
        "tasks": "Tasks:\n1. Follow @BirdzCoin\n2. Retweet pinned post",
        "daily_tasks": "*Daily Tasks*\nComplete these tasks and submit your username as proof:\n\n{daily_tasks}\n\n*Submission Format*: Enter task ID and username (e.g., '1 @username')",
        "claim": "Claim your {amount} Birdz Coins!",
        "balance": "Your Birdz Coin balance: {balance}",
        "task_completed": "Task '{task_description}' submitted! Awaiting admin approval.",
        "task_approved": "Task '{task_description}' approved! +{reward} Birdz Coins",
        "kyc_start": "Please provide your Telegram link (e.g., @username or https://t.me/username) to start KYC verification:",
        "kyc_complete": "KYC submitted successfully! Awaiting admin verification.\nDetails:\nTelegram: {telegram}\nX: {x_link}\nWallet: {wallet} ({chain})",
        "campaign_set": "Campaign '{name}' set! Start: {start}, End: {end}, Tokens: {tokens}",
        "view_blacklist": "Blacklisted Wallets:\n{wallets}",
        "view_whitelist": "Whitelisted Wallets:\n{wallets}",
        "pending_referrals": "Pending Referrals:\n{referrals}",
        "pending_tasks": "Pending Tasks:\n{tasks}",
        "user_details": "User Details:\nID: {user_id}\nUsername: {username}\nBalance: {balance}\nKYC: {kyc_status}\nWallet: {wallet} ({chain})",
        "contract_updated": "Token contract address updated to: {address} for token ID {token_id}, tier {tier}",
        "distribution_amount_updated": "Distribution amount updated for token {token_id}, tier {tier} to {amount}"
    }
}

# Rate Limiting (unchanged)
CALLS_PER_MINUTE = 10
PERIOD = 60

@sleep_and_retry
@limits(calls=CALLS_PER_MINUTE, period=PERIOD)
def rate_limited_request(url, payload):
    return requests.post(url, json=payload).json()

# Unified Context Class (unchanged)
class BotContext:
    def __init__(self, platform: str, user_data: dict = None):
        self.platform = platform
        self.user_data = user_data or {}
        self.bot = None

    async def send_message(self, chat_id: str, text: str, reply_markup=None):
        try:
            if self.platform == "telegram":
                format_args = self.user_data.get("format_args", {})
                if any(placeholder in text for placeholder in ["{balance}", "{ref_link}", "{"]):
                    formatted_text = text.format(**format_args)
                else:
                    formatted_text = text
                escaped_text = re.sub(r'([_*[\]()~`>#+\-=|{}.!])', r'\\\1', formatted_text)
                await asyncio.sleep(0.5)
                await self.bot.send_message(chat_id=chat_id, text=escaped_text, reply_markup=reply_markup, parse_mode='MarkdownV2')
            elif self.platform == "discord":
                channel = self.bot.get_channel(int(chat_id)) if chat_id.isdigit() else await self.bot.fetch_user(int(chat_id))
                if not channel:
                    raise Exception(f"Invalid chat_id: {chat_id}")
                if reply_markup:
                    text += "\n\nOptions:\n" + "\n".join([f"- {btn[0].text} (!Birdz {btn[0].callback_data})" for btn in reply_markup.inline_keyboard])
                await asyncio.sleep(0.5)
                await channel.send(text)
            logger.info(f"Message sent to {chat_id} on {self.platform}: {text[:50]}...")
        except Exception as e:
            logger.error(f"Error in send_message: {str(e)}")
            raise

    async def send_document(self, chat_id: str, document):
        if self.platform == "telegram":
            await self.bot.send_document(chat_id=chat_id, document=document)
        elif self.platform == "discord":
            channel = self.bot.get_channel(int(chat_id)) if chat_id.isdigit() else await self.bot.fetch_user(int(chat_id))
            if channel:
                await channel.send(file=discord.File(document))

# Helper Functions (unchanged except where noted)
def is_admin(user_id: str) -> bool:
    cursor.execute("SELECT role, permissions FROM admins WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result is not None

def is_super_admin(user_id: str) -> bool:
    cursor.execute("SELECT role FROM admins WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result and result[0] == "super_admin"

def has_permission(user_id: str, permission: str) -> bool:
    cursor.execute("SELECT role, permissions FROM admins WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result:
        return False
    role, permissions = result
    if role == "super_admin":
        return True
    if permissions == "all":
        return True
    return permission in permissions.split(",")

def generate_referral_code(user_id):
    return f"https://t.me/{BOT_USERNAME}?start={user_id}" if BOT_USERNAME else f"!start {user_id}"

def get_user_language(user_id: str) -> str:
    cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result and result[0] in LANGUAGES else "en"

def get_user_balance(user_id: str) -> float:
    cursor.execute("SELECT Birdz_balance FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else 0.0

def update_user_balance(user_id: str, amount: float):
    cursor.execute("UPDATE users SET Birdz_balance = Birdz_balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

def is_valid_telegram_link(link: str) -> bool:
    return bool(re.match(r"^(@[a-zA-Z0-9_]{5,32}|https://t\.me/[a-zA-Z0-9_]{5,32})$", link))

def is_valid_x_link(link: str) -> bool:
    return bool(re.match(r"^(@[a-zA-Z0-9_]{1,15}|https://x\.com/[a-zA-Z0-9_]{1,15})$", link))

def is_valid_address(wallet: str, chain: str) -> bool:
    if chain in ["ETH", "BSC"] and wallet.startswith("0x") and len(wallet) == 42:
        return web3_eth.is_address(wallet)
    if chain == "SOL" and 43 <= len(wallet) <= 44:
        try:
            Pubkey.from_string(wallet)
            return True
        except:
            return False
    if chain == "XRP" and 25 <= len(wallet) <= 35 and wallet.startswith("r"):
        try:
            from xrpl.core import addresscodec
            return addresscodec.is_valid_classic_address(wallet)
        except:
            return False
    return False

def check_mandatory_tasks(user_id: str) -> bool:
    cursor.execute("SELECT id FROM daily_tasks WHERE mandatory = 1")
    mandatory_tasks = [row[0] for row in cursor.fetchall()]
    for task_id in mandatory_tasks:
        cursor.execute("SELECT status FROM task_completions WHERE user_id = ? AND task_id = ? AND status = 'approved'", (user_id, task_id))
        if not cursor.fetchone():
            return False
    return True

def check_kyc_status(user_id: str) -> str:
    cursor.execute("SELECT kyc_status FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else "pending"

def has_seen_menu(user_id: str) -> bool:
    cursor.execute("SELECT has_seen_menu FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] == 1 if result else False

def has_joined_groups(user_id: str) -> bool:
    cursor.execute("SELECT joined_groups FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] == 1 if result else False

async def check_eligibility(wallet: str, chain: str) -> tuple[int, float]:
    try:
        token_balance = 0.0
        tier = 0
        if chain == "ETH":
            token_balance = web3_eth.eth.get_balance(wallet) / 10**18
            tier = min(3, max(1, int(token_balance // 100)))
        elif chain == "BSC":
            token_balance = web3_bsc.eth.get_balance(wallet) / 10**18
            tier = min(3, max(1, int(token_balance // 100)))
        elif chain == "SOL":
            tier, token_balance = 1, 0.0  # Placeholder
        elif chain == "XRP":
            response = xrp_client.request({"method": "account_info", "params": [{"account": wallet}]})
            if "error" in response.result:
                tier, token_balance = 0, 0.0
            else:
                xrp_balance = float(response.result["account_data"]["Balance"]) / 10**6
                tier = min(3, max(1, int(xrp_balance // 10)))
                token_balance = xrp_balance
        min_balance = float(cursor.execute("SELECT value FROM config WHERE key = 'min_token_balance'").fetchone()[0])
        return tier if tier > 0 or token_balance >= min_balance else 0, token_balance
    except Exception as e:
        logger.error(f"Eligibility check failed: {str(e)}")
        return 0, 0.0

def get_main_menu(user_id, lang):
    keyboard = [
        [InlineKeyboardButton("Join Airdrop", callback_data="join_airdrop")],
        [InlineKeyboardButton("Check Balance", callback_data="balance"),
         InlineKeyboardButton("Terms", callback_data="terms")],
        [InlineKeyboardButton("KYC", callback_data="kyc_start"),
         InlineKeyboardButton("Submit Wallet", callback_data="submit_wallet")],
        [InlineKeyboardButton("Tasks", callback_data="tasks"),
         InlineKeyboardButton("Daily Tasks", callback_data="daily_tasks")],
        [InlineKeyboardButton("Refer", callback_data="refer"),
         InlineKeyboardButton("Claim Tokens", callback_data="claim_tokens")],
        [InlineKeyboardButton("Leaderboard", callback_data="leaderboard")]
    ]
    if is_admin(user_id):
        admin_buttons = []
        if is_super_admin(user_id):
            admin_buttons.extend([
                [InlineKeyboardButton("Manage Admins", callback_data="manage_admins")],
                [InlineKeyboardButton("Admin: Export Data", callback_data="export_data")]
            ])
        
        if has_permission(user_id, "distribute"):
            admin_buttons.extend([
                [InlineKeyboardButton("Distribute BirdzCoin Tier 1", callback_data="start_distribution_1_tier1")],
                [InlineKeyboardButton("Distribute BirdzCoin Tier 2", callback_data="start_distribution_1_tier2")],
                [InlineKeyboardButton("Distribute BirdzCoin Tier 3", callback_data="start_distribution_1_tier3")]
            ])
            
            cursor.execute("SELECT token_id, name FROM tokens WHERE token_id NOT IN (1, 2, 3, 4, 5, 6, 8, 9)")
            tokens = cursor.fetchall()
            distribution_buttons = [
                InlineKeyboardButton(f"Distribute {token[1]} (ID: {token[0]})", callback_data=f"start_distribution_{token[0]}")
                for token in tokens
            ]
            admin_buttons.extend([distribution_buttons])
        
        if has_permission(user_id, "manage_tasks"):
            admin_buttons.extend([
                [InlineKeyboardButton("Add Task", callback_data="add_task"),
                 InlineKeyboardButton("Edit Task", callback_data="edit_task"),
                 InlineKeyboardButton("Delete Task", callback_data="delete_task")],
                [InlineKeyboardButton("Approve Tasks", callback_data="approve_tasks")]
            ])
        
        if has_permission(user_id, "manage_users"):
            admin_buttons.extend([
                [InlineKeyboardButton("View Users", callback_data="view_users"),
                 InlineKeyboardButton("Reset User", callback_data="reset_user")],
                [InlineKeyboardButton("Approve Referrals", callback_data="approve_referrals")]
            ])
        
        if has_permission(user_id, "manage_config"):
            admin_buttons.extend([
                [InlineKeyboardButton("Admin: Set Config", callback_data="set_config")],
                [InlineKeyboardButton("Admin: Set Amount", callback_data="set_distribution_amount"),
                 InlineKeyboardButton("Set Bulk Amounts", callback_data="set_bulk_amounts")],
                [InlineKeyboardButton("Change Contract", callback_data="change_contract"),
                 InlineKeyboardButton("Set Token Amount", callback_data="set_token_amount")],
                [InlineKeyboardButton("Set Campaign", callback_data="set_campaign"),
                 InlineKeyboardButton("Edit Campaign", callback_data="edit_campaign")],
                [InlineKeyboardButton("Delete Campaign", callback_data="delete_campaign")]
            ])
        
        if has_permission(user_id, "manage_blacklist"):
            admin_buttons.extend([
                [InlineKeyboardButton("Admin: Blacklist", callback_data="blacklist"),
                 InlineKeyboardButton("Admin: Whitelist", callback_data="whitelist")],
                [InlineKeyboardButton("View Blacklist", callback_data="view_blacklist"),
                 InlineKeyboardButton("View Whitelist", callback_data="view_whitelist")]
            ])
        
        keyboard.extend(admin_buttons)
    return InlineKeyboardMarkup(keyboard)

# Core Bot Logic
class AirdropBot:
    def __init__(self):
        self.telegram_app = None
        self.discord_bot = None

    async def start(self, update: Union[Update, discord.Message], context: BotContext):
        user_id = str(update.message.from_user.id if context.platform == "telegram" else update.author.id)
        user_name = update.message.from_user.first_name if context.platform == "telegram" else update.author.name
        lang = get_user_language(user_id)
        chat_id = str(update.message.chat_id if context.platform == "telegram" else update.channel.id)

        referral_code = generate_referral_code(user_id)
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username, language, referral_code, kyc_status, agreed_terms, has_seen_menu, joined_groups) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (user_id, user_name, lang, referral_code, "pending", 0, 0, 0))
        conn.commit()

        args = update.message.text.split() if context.platform == "telegram" else update.content.split()
        if len(args) > 1 and args[1].startswith("start="):
            referrer_id = args[1].split("=")[1]
            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
            referrer = cursor.fetchone()
            if referrer and referrer[0] != user_id:
                cursor.execute("SELECT referee_id FROM referrals WHERE referee_id = ?", (user_id,))
                if not cursor.fetchone():
                    cursor.execute("INSERT OR IGNORE INTO referrals (referrer_id, referee_id, timestamp) VALUES (?, ?, ?)",
                                   (referrer[0], user_id, datetime.utcnow().isoformat()))
                    cursor.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer[0], user_id))
                    conn.commit()
                    await context.send_message(referrer[0], LANGUAGES[lang]["referral_pending"].format(referee=user_name))

        if not has_seen_menu(user_id):
            keyboard = [[InlineKeyboardButton("Continue", callback_data="check_groups")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["mandatory_rules"], reply_markup)
        else:
            balance = get_user_balance(user_id)
            reply_markup = get_main_menu(user_id, lang)
            context.user_data["format_args"] = {"balance": balance, "ref_link": referral_code}
            await context.send_message(chat_id, LANGUAGES[lang]["welcome"], reply_markup)

    async def button_handler(self, update: Union[Update, discord.Message], context: BotContext):
        user_id = str(update.callback_query.from_user.id if context.platform == "telegram" else update.author.id)
        lang = get_user_language(user_id)
        chat_id = str(update.callback_query.message.chat_id if context.platform == "telegram" else update.channel.id)
        data = update.callback_query.data if context.platform == "telegram" else update.content.split()[1] if len(update.content.split()) > 1 else ""

        if data == "start":
            if not has_seen_menu(user_id):
                keyboard = [[InlineKeyboardButton("Continue", callback_data="check_groups")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["mandatory_rules"], reply_markup)
            else:
                balance = get_user_balance(user_id)
                referral_code = generate_referral_code(user_id)
                reply_markup = get_main_menu(user_id, lang)
                context.user_data["format_args"] = {"balance": balance, "ref_link": referral_code}
                await context.send_message(chat_id, LANGUAGES[lang]["welcome"], reply_markup)
            context.user_data.clear()

        elif data == "check_groups":
            if has_joined_groups(user_id):
                cursor.execute("UPDATE users SET has_seen_menu = 1 WHERE user_id = ?", (user_id,))
                conn.commit()
                balance = get_user_balance(user_id)
                referral_code = generate_referral_code(user_id)
                reply_markup = get_main_menu(user_id, lang)
                context.user_data["format_args"] = {"balance": balance, "ref_link": referral_code}
                await context.send_message(chat_id, LANGUAGES[lang]["welcome"], reply_markup)
            else:
                keyboard = [[InlineKeyboardButton("I've Joined Both Groups", callback_data="confirm_groups")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["confirm_groups"], reply_markup)

        elif data == "confirm_groups":
            cursor.execute("UPDATE users SET joined_groups = 1, has_seen_menu = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            balance = get_user_balance(user_id)
            referral_code = generate_referral_code(user_id)
            reply_markup = get_main_menu(user_id, lang)
            context.user_data["format_args"] = {"balance": balance, "ref_link": referral_code}
            await context.send_message(chat_id, LANGUAGES[lang]["welcome"], reply_markup)

        elif data == "join_airdrop":
            if not check_mandatory_tasks(user_id):
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Please complete all mandatory tasks first.", reply_markup)
            else:
                await context.send_message(chat_id, "You've joined the airdrop! Submit your wallet to proceed.", reply_markup=get_main_menu(user_id, lang))

        elif data == "balance":
            balance = get_user_balance(user_id)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.user_data["format_args"] = {"balance": balance}
            await context.send_message(chat_id, LANGUAGES[lang]["balance"], reply_markup)

        elif data == "terms":
            keyboard = [[InlineKeyboardButton(" Agree", callback_data="agree_terms")],
                        [InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["terms"], reply_markup)

        elif data == "agree_terms":
            cursor.execute("UPDATE users SET agreed_terms = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Terms agreed! Proceed with other actions.", reply_markup)

        elif data == "kyc_start":
            if check_kyc_status(user_id) == "verified":
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Your KYC is already verified!", reply_markup)
            else:
                context.user_data['kyc_step'] = "telegram"
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["kyc_start"], reply_markup)

        elif data == "submit_wallet":
            keyboard = [
                [InlineKeyboardButton("ETH", callback_data="wallet_eth"),
                 InlineKeyboardButton("BSC", callback_data="wallet_bsc"),
                 InlineKeyboardButton("SOL", callback_data="wallet_sol"),
                 InlineKeyboardButton("XRP", callback_data="wallet_xrp")],
                [InlineKeyboardButton("Back to Menu", callback_data="start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.user_data['awaiting_wallet'] = True
            await context.send_message(chat_id, LANGUAGES[lang]["usage"], reply_markup)

        elif data.startswith("wallet_"):
            chain = data.split("_")[1].upper()
            context.user_data['chain'] = chain
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Enter your {chain} wallet address:", reply_markup)

        elif data == "tasks":
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["tasks"], reply_markup)

        elif data == "daily_tasks":
            cursor.execute("SELECT id, description, reward, task_link FROM daily_tasks WHERE active = 1")
            tasks = cursor.fetchall()
            daily_tasks_str = "\n".join([f"{task[0]}. {task[1]} - {task[2]} Birdz Coins ({task[3]})" for task in tasks])
            context.user_data["format_args"] = {"daily_tasks": daily_tasks_str}
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["daily_tasks"], reply_markup)

        elif data == "refer":
            referral_code = generate_referral_code(user_id)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Your referral link: {referral_code}", reply_markup)

        elif data == "claim_tokens":
            cursor.execute("SELECT amount FROM distributions WHERE user_id = ? AND status = 'claimable'", (user_id,))
            distribution = cursor.fetchone()
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            if not distribution:
                await context.send_message(chat_id, "No claimable Birdz Coins found.", reply_markup)
            else:
                amount = distribution[0]
                cursor.execute("UPDATE distributions SET status = 'claimed' WHERE user_id = ?", (user_id,))
                update_user_balance(user_id, amount)
                conn.commit()
                context.user_data["format_args"] = {"amount": amount}
                await context.send_message(chat_id, LANGUAGES[lang]["claim"], reply_markup)

        elif data.startswith("start_distribution_"):
            if not is_admin(user_id):
                await context.send_message(chat_id, "You don't have permission to distribute tokens.")
                return
                
            parts = data.split("_")
            token_id = int(parts[2])
            tier = parts[3] if len(parts) > 3 else None
            
            if tier:
                tier_num = int(tier.replace("tier", ""))
                await self.calculate_airdrop_by_tier(1, token_id, tier_num)
            else:
                await calculate_airdrop(1, token_id)
                
            await self.distribute_tokens(chat_id, context, token_id, lang)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Token distribution completed!", reply_markup)

        elif data == "distribute_all" and is_admin(user_id):
            await calculate_airdrop_all(1, "1")
            await self.distribute_tokens(chat_id, context, "1", lang)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Distribution to all users started!", reply_markup)

        elif data == "export_data" and is_admin(user_id):
            wb = Workbook()
            ws = wb.active
            ws.append(["User ID", "Wallet", "Chain", "Amount", "Status", "Tx Hash"])
            cursor.execute("SELECT user_id, wallet, chain, amount, status, tx_hash FROM distributions")
            for row in cursor.fetchall():
                ws.append(row)
            wb.save("airdrop_log.xlsx")
            await context.send_document(chat_id, open("airdrop_log.xlsx", "rb"))
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Data exported!", reply_markup)

        elif data == "blacklist" and is_admin(user_id):
            context.user_data['awaiting_blacklist'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter wallet to blacklist:", reply_markup)

        elif data == "whitelist" and is_admin(user_id):
            context.user_data['awaiting_whitelist'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter wallet to whitelist:", reply_markup)

        elif data == "view_blacklist" and is_admin(user_id):
            cursor.execute("SELECT wallet FROM blacklist")
            wallets = [row[0] for row in cursor.fetchall()]
            response = "\n".join(wallets) if wallets else "No wallets blacklisted."
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.user_data["format_args"] = {"wallets": response}
            await context.send_message(chat_id, LANGUAGES[lang]["view_blacklist"], reply_markup)

        elif data == "view_whitelist" and is_admin(user_id):
            cursor.execute("SELECT wallet FROM whitelist")
            wallets = [row[0] for row in cursor.fetchall()]
            response = "\n".join(wallets) if wallets else "No wallets whitelisted."
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.user_data["format_args"] = {"wallets": response}
            await context.send_message(chat_id, LANGUAGES[lang]["view_whitelist"], reply_markup)

        elif data == "set_distribution_amount" and is_admin(user_id):
            context.user_data['awaiting_amount'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter user_id and amount (e.g., '12345 500'):", reply_markup)

        elif data == "set_bulk_amounts" and is_admin(user_id):
            context.user_data['awaiting_bulk_amounts'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter amounts as 'user_id:amount' pairs (e.g., '123:500 456:1000'):", reply_markup)

        elif data == "set_config" and is_admin(user_id):
            context.user_data['awaiting_config'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter config key and value (e.g., total_supply 2000000):", reply_markup)

        elif data == "approve_referrals" and is_admin(user_id):
            cursor.execute("SELECT referrer_id, referee_id, timestamp FROM referrals WHERE status = 'pending'")
            referrals = cursor.fetchall()
            if not referrals:
                await context.send_message(chat_id, "No pending referrals.", reply_markup=get_main_menu(user_id, lang))
            else:
                referral_str = "\n".join([f"Ref: {r[0]} -> Referee: {r[1]} ({r[2]})" for r in referrals])
                keyboard = [[InlineKeyboardButton("Approve All", callback_data="approve_all_referrals")],
                            [InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                context.user_data["format_args"] = {"referrals": referral_str}
                await context.send_message(chat_id, LANGUAGES[lang]["pending_referrals"], reply_markup)
                context.user_data['awaiting_referral_approval'] = True

        elif data == "approve_all_referrals" and is_admin(user_id):
            cursor.execute("SELECT referrer_id, referee_id FROM referrals WHERE status = 'pending'")
            referrals = cursor.fetchall()
            bonus = float(cursor.execute("SELECT value FROM config WHERE key = 'referral_bonus'").fetchone()[0])
            for referrer_id, referee_id in referrals:
                cursor.execute("UPDATE referrals SET status = 'approved' WHERE referee_id = ?", (referee_id,))
                update_user_balance(referrer_id, bonus)
                cursor.execute("SELECT username FROM users WHERE user_id = ?", (referee_id,))
                referee_name = cursor.fetchone()[0]
                context.user_data["format_args"] = {"bonus": bonus, "referee": referee_name}
                await context.send_message(referrer_id, LANGUAGES[lang]["referral_bonus"])
            conn.commit()
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "All referrals approved!", reply_markup)
            context.user_data['awaiting_referral_approval'] = False

        elif data == "approve_tasks" and is_admin(user_id):
            cursor.execute("SELECT user_id, task_id, completion_date, username FROM task_completions WHERE status = 'pending'")
            tasks = cursor.fetchall()
            if not tasks:
                await context.send_message(chat_id, "No pending tasks.", reply_markup=get_main_menu(user_id, lang))
            else:
                task_str = "\n".join([f"User: {t[0]} - Task {t[1]} - {t[3]} ({t[2]})" for t in tasks])
                keyboard = [[InlineKeyboardButton("Approve All", callback_data="approve_all_tasks")],
                            [InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                context.user_data["format_args"] = {"tasks": task_str}
                await context.send_message(chat_id, LANGUAGES[lang]["pending_tasks"], reply_markup)
                context.user_data['awaiting_task_approval'] = True

        elif data == "approve_all_tasks" and is_admin(user_id):
            cursor.execute("SELECT user_id, task_id, completion_date FROM task_completions WHERE status = 'pending'")
            tasks = cursor.fetchall()
            for user_id_task, task_id, completion_date in tasks:
                cursor.execute("SELECT description, reward FROM daily_tasks WHERE id = ?", (task_id,))
                task_data = cursor.fetchone()
                if task_data:
                    description, reward = task_data
                    cursor.execute("UPDATE task_completions SET status = 'approved' WHERE user_id = ? AND task_id = ? AND completion_date = ?",
                                   (user_id_task, task_id, completion_date))
                    update_user_balance(user_id_task, reward)
                    context.user_data["format_args"] = {"task_description": description, "reward": reward}
                    await context.send_message(user_id_task, LANGUAGES[lang]["task_approved"])
            conn.commit()
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "All tasks approved!", reply_markup)
            context.user_data['awaiting_task_approval'] = False

        elif data == "add_task" and is_admin(user_id):
            context.user_data['awaiting_task_add'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter task details (description reward mandatory task_link, e.g., 'Join Discord 15 0 https://discord.gg/example'):", reply_markup)

        elif data == "edit_task" and is_admin(user_id):
            context.user_data['awaiting_task_edit'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter task ID and new details (id description reward mandatory task_link, e.g., '1 Join Discord 15 0 https://discord.gg/example'):", reply_markup)

        elif data == "delete_task" and is_admin(user_id):
            context.user_data['awaiting_task_delete'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter task ID to delete:", reply_markup)

        elif data == "set_campaign" and is_admin(user_id):
            context.user_data['awaiting_campaign'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu",callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter campaign details (name start_date end_date total_tokens, e.g., 'New Campaign 2025-04-01 2025-04-07 500000'):", reply_markup)

        elif data == "view_users" and is_admin(user_id):
            cursor.execute("SELECT user_id, username, Birdz_balance, kyc_status, kyc_wallet, kyc_chain FROM users")
            users = cursor.fetchall()
            user_details = "\n".join([f"ID: {u[0]}, Username: {u[1]}, Balance: {u[2]}, KYC: {u[3]}, Wallet: {u[4] or 'N/A'} ({u[5] or 'N/A'})" for u in users])
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.user_data["format_args"] = {"user_id": "", "username": "", "balance": "", "kyc_status": "", "wallet": "", "chain": ""}
            await context.send_message(chat_id, f"Users:\n{user_details}", reply_markup)

        elif data == "reset_user" and is_admin(user_id):
            context.user_data['awaiting_user_reset'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter user ID to reset:", reply_markup)

        elif data == "leaderboard":
            leaderboard_text = await get_leaderboard_text(lang)
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, leaderboard_text, reply_markup)

        elif data == "manage_admins" and is_super_admin(user_id):
            cursor.execute("SELECT user_id, username, role, added_by, added_at FROM admins")
            admins = cursor.fetchall()
            admin_list = "\n".join([f"ID: {a[0]}, Username: {a[1]}, Role: {a[2]}, Added by: {a[3]}, Added at: {a[4]}" for a in admins])
            keyboard = [
                [InlineKeyboardButton("Add Admin", callback_data="add_admin")],
                [InlineKeyboardButton("Remove Admin", callback_data="remove_admin")],
                [InlineKeyboardButton("Edit Permissions", callback_data="edit_permissions")],
                [InlineKeyboardButton("Back to Menu", callback_data="start")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Current Admins:\n{admin_list}", reply_markup)

        elif data == "add_admin" and is_super_admin(user_id):
            context.user_data['awaiting_admin_add'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter user ID and role (e.g., '12345 admin'):", reply_markup)

        elif data == "remove_admin" and is_super_admin(user_id):
            context.user_data['awaiting_admin_remove'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter user ID to remove from admin:", reply_markup)

        elif data == "edit_permissions" and is_super_admin(user_id):
            context.user_data['awaiting_permission_edit'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter user ID and permissions (e.g., '12345 distribute,manage_tasks'):", reply_markup)

        elif data == "edit_campaign" and is_admin(user_id):
            cursor.execute("SELECT id, name, start_date, end_date, total_tokens FROM campaigns WHERE active = 1")
            campaigns = cursor.fetchall()
            if not campaigns:
                await context.send_message(chat_id, "No active campaigns found.", reply_markup=get_main_menu(user_id, lang))
                return
            
            campaign_list = "\n".join([f"ID: {c[0]}, Name: {c[1]}, Start: {c[2]}, End: {c[3]}, Tokens: {c[4]}" for c in campaigns])
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Active Campaigns:\n{campaign_list}\n\nEnter campaign ID and new details (id name start_date end_date total_tokens):", reply_markup)
            context.user_data['awaiting_campaign_edit'] = True

        elif data == "delete_campaign" and is_admin(user_id):
            cursor.execute("SELECT id, name FROM campaigns WHERE active = 1")
            campaigns = cursor.fetchall()
            if not campaigns:
                await context.send_message(chat_id, "No active campaigns found.", reply_markup=get_main_menu(user_id, lang))
                return
            
            campaign_list = "\n".join([f"ID: {c[0]}, Name: {c[1]}" for c in campaigns])
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Active Campaigns:\n{campaign_list}\n\nEnter campaign ID to delete:", reply_markup)
            context.user_data['awaiting_campaign_delete'] = True

        elif data == "change_contract" and is_admin(user_id):
            context.user_data['awaiting_contract_change'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter token ID, tier, and new contract address (e.g., '1 2 0xNewAddress'):", reply_markup)

        elif data == "set_token_amount" and is_admin(user_id):
            context.user_data['awaiting_token_amount'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "Enter token ID, tier, amount, and contract address (e.g., '1 2 2500 0xNewAddress'):", reply_markup)

        elif data == "view_admins" and is_super_admin(user_id):
            await view_admins(chat_id)

        if context.platform == "telegram":
            await update.callback_query.answer()

    async def handle_message(self, update: Union[Update, discord.Message], context: BotContext):
        user_id = str(update.message.from_user.id if context.platform == "telegram" else update.author.id)
        lang = get_user_language(user_id)
        chat_id = str(update.message.chat_id if context.platform == "telegram" else update.channel.id)
        text = update.message.text.strip() if context.platform == "telegram" else update.content.strip()

        if context.user_data.get('kyc_step') == "telegram":
            if not is_valid_telegram_link(text):
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Invalid Telegram link.", reply_markup)
                return
            context.user_data['kyc_telegram_link'] = text
            context.user_data['kyc_step'] = "x_link"
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"Telegram link received: {text}. Now provide your X link:", reply_markup)
            return

        elif context.user_data.get('kyc_step') == "x_link":
            if not is_valid_x_link(text):
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Invalid X link.", reply_markup)
                return
            context.user_data['kyc_x_link'] = text
            context.user_data['kyc_step'] = "wallet"
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "X link received: {}. Now provide your wallet address (e.g., 'ETH 0x...')".format(text), reply_markup)
            return

        elif context.user_data.get('kyc_step') == "wallet":
            try:
                chain, wallet = text.split(maxsplit=1)
                chain = chain.upper()
                if chain not in ["ETH", "BSC", "SOL", "XRP"] or not is_valid_address(wallet, chain):
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, LANGUAGES[lang]["invalid_address"].format(chain=chain), reply_markup)
                    return
                submission_time = datetime.utcnow().isoformat()
                cursor.execute("UPDATE users SET kyc_telegram_link = ?, kyc_x_link = ?, kyc_wallet = ?, kyc_chain = ?, kyc_status = 'submitted', kyc_submission_time = ? WHERE user_id = ?",
                               (context.user_data['kyc_telegram_link'], context.user_data['kyc_x_link'], wallet, chain, submission_time, user_id))
                cursor.execute("INSERT OR IGNORE INTO submissions (user_id, wallet, chain, timestamp) VALUES (?, ?, ?, ?)",
                               (user_id, wallet, chain, submission_time))
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                context.user_data["format_args"] = {
                    "telegram": context.user_data['kyc_telegram_link'],
                    "x_link": context.user_data['kyc_x_link'],
                    "wallet": wallet,
                    "chain": chain
                }
                await context.send_message(chat_id, LANGUAGES[lang]["kyc_complete"], reply_markup)
                context.user_data.clear()
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Invalid wallet format.", reply_markup)
            return

        elif context.user_data.get('awaiting_wallet'):
            wallet = text
            chain = context.user_data.get('chain')
            if not is_valid_address(wallet, chain):
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["invalid_address"].format(chain=chain), reply_markup)
                context.user_data['awaiting_wallet'] = False
                return
            cursor.execute("SELECT wallet FROM blacklist WHERE wallet = ?", (wallet,))
            if cursor.fetchone():
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["blacklisted"], reply_markup)
                context.user_data['awaiting_wallet'] = False
                return
            cursor.execute("SELECT wallet FROM submissions WHERE user_id = ?", (user_id,))
            if cursor.fetchone():
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["already_submitted"], reply_markup)
                context.user_data['awaiting_wallet'] = False
                return
            captcha = random.randint(1, 10)
            cursor.execute("REPLACE INTO captchas (user_id, captcha, timestamp) VALUES (?, ?, ?)",
                           (user_id, captcha, datetime.utcnow().isoformat()))
            cursor.execute("REPLACE INTO submissions (user_id, wallet, chain, timestamp) VALUES (?, ?, ?, ?)",
                           (user_id, wallet, chain, datetime.utcnow().isoformat()))
            conn.commit()
            context.user_data['awaiting_wallet'] = False
            context.user_data['awaiting_captcha'] = True
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            context.user_data["format_args"] = {"captcha": captcha}
            await context.send_message(chat_id, LANGUAGES[lang]["captcha"], reply_markup)

        elif context.user_data.get('awaiting_captcha'):
            try:
                user_answer = int(text)
                cursor.execute("SELECT captcha FROM captchas WHERE user_id = ?", (user_id,))
                result = cursor.fetchone()
                if not result or user_answer != result[0] + 5:
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, "Wrong answer.", reply_markup)
                else:
                    await self.verify_wallet(user_id, chat_id, context, lang)
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Please enter a number.", reply_markup)
            context.user_data['awaiting_captcha'] = False

        elif context.user_data.get('awaiting_blacklist'):
            wallet = text
            cursor.execute("INSERT OR IGNORE INTO blacklist (wallet) VALUES (?)", (wallet,))
            conn.commit()
            context.user_data['awaiting_blacklist'] = False
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"{wallet} blacklisted.", reply_markup)

        elif context.user_data.get('awaiting_whitelist'):
            wallet = text
            cursor.execute("INSERT OR IGNORE INTO whitelist (wallet) VALUES (?)", (wallet,))
            conn.commit()
            context.user_data['awaiting_whitelist'] = False
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, f"{wallet} whitelisted.", reply_markup)

        elif context.user_data.get('awaiting_amount'):
            try:
                user_id_to_set, amount = text.split()
                amount = float(amount)
                cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id_to_set,))
                result = cursor.fetchone()
                if result:
                    wallet, chain = result
                    cursor.execute("REPLACE INTO distributions (user_id, wallet, chain, amount, status) VALUES (?, ?, ?, ?, ?)",
                                   (user_id_to_set, wallet, chain, amount, "pending"))
                    conn.commit()
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, f"Set {amount} tokens for user {user_id_to_set}", reply_markup)
                else:
                    await context.send_message(chat_id, "User has not submitted a wallet.", reply_markup)
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Format: user_id amount", reply_markup)
            context.user_data['awaiting_amount'] = False

        elif context.user_data.get('awaiting_bulk_amounts'):
            try:
                pairs = text.split()
                for pair in pairs:
                    user_id_to_set, amount = pair.split(":")
                    amount = float(amount)
                    cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id_to_set,))
                    result = cursor.fetchone()
                    if result:
                        wallet, chain = result
                        cursor.execute("REPLACE INTO distributions (user_id, wallet, chain, amount, status) VALUES (?, ?, ?, ?, ?)",
                                       (user_id_to_set, wallet, chain, amount, "pending"))
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Bulk amounts set!", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: user_id:amount user_id:amount", reply_markup)
            context.user_data['awaiting_bulk_amounts'] = False

        elif context.user_data.get('awaiting_config'):
            try:
                key, value = text.split()
                cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
                conn.commit()
                context.user_data['awaiting_config'] = False
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Set {key} = {value}", reply_markup)
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Format: key value", reply_markup)

        elif context.user_data.get('awaiting_task_add'):
            try:
                description, reward, mandatory, task_link = text.split(maxsplit=3)
                reward = float(reward)
                mandatory = int(mandatory)
                cursor.execute("INSERT INTO daily_tasks (description, reward, mandatory, task_link, active) VALUES (?, ?, ?, ?, 1)",
                               (description, reward, mandatory, task_link))
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Task '{description}' added!", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: description reward mandatory task_link", reply_markup)
            context.user_data['awaiting_task_add'] = False

        elif context.user_data.get('awaiting_task_edit'):
            try:
                task_id, description, reward, mandatory, task_link = text.split(maxsplit=4)
                task_id = int(task_id)
                reward = float(reward)
                mandatory = int(mandatory)
                cursor.execute("UPDATE daily_tasks SET description = ?, reward = ?, mandatory = ?, task_link = ? WHERE id = ?",
                               (description, reward, mandatory, task_link, task_id))
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Task ID {task_id} updated!", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: id description reward mandatory task_link", reply_markup)
            context.user_data['awaiting_task_edit'] = False

        elif context.user_data.get('awaiting_task_delete'):
            try:
                task_id = int(text)
                cursor.execute("DELETE FROM daily_tasks WHERE id = ?", (task_id,))
                cursor.execute("DELETE FROM task_completions WHERE task_id = ?", (task_id,))
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Task ID {task_id} deleted!", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: task_id", reply_markup)
            context.user_data['awaiting_task_delete'] = False

        elif context.user_data.get('awaiting_campaign'):
            try:
                name, start_date, end_date, total_tokens = text.split(maxsplit=3)
                total_tokens = float(total_tokens)
                cursor.execute("INSERT INTO campaigns (name, start_date, end_date, total_tokens, active) VALUES (?, ?, ?, ?, 1)",
                               (name, start_date, end_date, total_tokens))
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                context.user_data["format_args"] = {"name": name, "start": start_date, "end": end_date, "tokens": total_tokens}
                await context.send_message(chat_id, LANGUAGES[lang]["campaign_set"], reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: name start_date end_date total_tokens", reply_markup)
            context.user_data['awaiting_campaign'] = False

        elif context.user_data.get('awaiting_user_reset'):
            try:
                reset_user_id = text
                cursor.execute("DELETE FROM users WHERE user_id = ?", (reset_user_id,))
                cursor.execute("DELETE FROM submissions WHERE user_id = ?", (reset_user_id,))
                cursor.execute("DELETE FROM eligible WHERE user_id = ?", (reset_user_id,))
                cursor.execute("DELETE FROM distributions WHERE user_id = ?", (reset_user_id,))
                cursor.execute("DELETE FROM referrals WHERE referrer_id = ? OR referee_id = ?", (reset_user_id, reset_user_id))
                cursor.execute("DELETE FROM task_completions WHERE user_id = ?", (reset_user_id,))
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"User {reset_user_id} reset!", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: user_id", reply_markup)
            context.user_data['awaiting_user_reset'] = False

        elif context.user_data.get('awaiting_admin_add'):
            try:
                user_id_to_add, role = text.split()
                await add_admin(user_id_to_add, role, user_id)  # Call the function to add admin
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Added {user_id_to_add} as {role}", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: user_id role", reply_markup)
            context.user_data['awaiting_admin_add'] = False

        elif context.user_data.get('awaiting_admin_remove'):
            try:
                user_id_to_remove = text
                if user_id_to_remove == ADMIN_ID:
                    await context.send_message(chat_id, "Cannot remove super admin.", reply_markup)
                else:
                    cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id_to_remove,))
                    conn.commit()
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, f"Removed admin {user_id_to_remove}", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: user_id", reply_markup)
            context.user_data['awaiting_admin_remove'] = False

        elif context.user_data.get('awaiting_permission_edit'):
            try:
                user_id_to_edit, permissions = text.split()
                if user_id_to_edit == ADMIN_ID:
                    await context.send_message(chat_id, "Cannot modify super admin permissions.", reply_markup)
                else:
                    cursor.execute("UPDATE admins SET permissions = ? WHERE user_id = ?", (permissions, user_id_to_edit))
                    conn.commit()
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, f"Updated permissions for {user_id_to_edit}", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: user_id permissions", reply_markup)
            context.user_data['awaiting_permission_edit'] = False

        elif text.startswith(("1 ", "2 ", "3 ")):
            try:
                task_id, username = text.split(maxsplit=1)
                task_id = int(task_id)
                cursor.execute("SELECT description FROM daily_tasks WHERE id = ? AND active = 1", (task_id,))
                task = cursor.fetchone()
                if task:
                    completion_date = datetime.utcnow().isoformat()
                    cursor.execute("INSERT OR IGNORE INTO task_completions (user_id, task_id, completion_date, username, status) VALUES (?, ?, ?, ?, ?)",
                                   (user_id, task_id, completion_date, username, "pending"))
                    conn.commit()
                    context.user_data["format_args"] = {"task_description": task[0]}
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, LANGUAGES[lang]["task_completed"], reply_markup)
            except ValueError:
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, "Invalid task format.", reply_markup)

        elif context.user_data.get('awaiting_campaign_edit'):
            try:
                campaign_id, name, start_date, end_date, total_tokens = text.split(maxsplit=4)
                campaign_id = int(campaign_id)
                total_tokens = float(total_tokens)
                cursor.execute("UPDATE campaigns SET name = ?, start_date = ?, end_date = ?, total_tokens = ? WHERE id = ?",
                               (name, start_date, end_date, total_tokens, campaign_id))
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Campaign ID {campaign_id} updated successfully!", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Invalid format. Please enter: id name start_date end_date total_tokens")
            context.user_data['awaiting_campaign_edit'] = False

        elif context.user_data.get('awaiting_campaign_delete'):
            try:
                campaign_id = int(text)
                cursor.execute("UPDATE campaigns SET active = 0 WHERE id = ?", (campaign_id,))
                conn.commit()
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, f"Campaign ID {campaign_id} deleted successfully!", reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Please enter a valid campaign ID")
            context.user_data['awaiting_campaign_delete'] = False

        elif context.user_data.get('awaiting_contract_change'):
            try:
                token_id, tier, new_address = text.split()
                token_id = int(token_id)
                tier = int(tier)
                if not (tier in [1, 2, 3] and (Web3.is_address(new_address) or new_address.startswith("r") or len(new_address) in [43, 44])):
                    await context.send_message(chat_id, "Invalid tier or contract address format")
                    return
                cursor.execute("UPDATE token_distributions SET contract_address = ? WHERE token_id = ? AND tier = ?", (new_address, token_id, tier))
                if cursor.rowcount == 0:
                    await context.send_message(chat_id, "Token ID and tier combination not found")
                else:
                    conn.commit()
                    context.user_data["format_args"] = {"address": new_address, "token_id": token_id, "tier": tier}
                    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.send_message(chat_id, "Contract Address Updated Successfully!", reply_markup)  # Success message
            except ValueError:
                await context.send_message(chat_id, "Format: token_id tier new_address")
            context.user_data['awaiting_contract_change'] = False

        elif context.user_data.get('awaiting_token_amount'):
            try:
                token_id, tier, amount, contract_address = text.split()
                token_id = int(token_id)
                tier = int(tier)
                amount = float(amount)
                if not (tier in [1, 2, 3] and (Web3.is_address(contract_address) or contract_address.startswith("r") or len(contract_address) in [43, 44])):
                    await context.send_message(chat_id, "Invalid tier or contract address format")
                    return
                cursor.execute("REPLACE INTO token_distributions (token_id, tier, amount, contract_address) VALUES (?, ?, ?, ?)",
                              (token_id, tier, amount, contract_address))
                conn.commit()
                context.user_data["format_args"] = {"token_id": token_id, "tier": tier, "amount": amount}
                keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.send_message(chat_id, LANGUAGES[lang]["distribution_amount_updated"], reply_markup)
            except ValueError:
                await context.send_message(chat_id, "Format: token_id tier amount contract_address")
            context.user_data['awaiting_token_amount'] = False

    async def verify_wallet(self, user_id, chat_id, context: BotContext, lang):
        cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        if not result:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, "No wallet submission found.", reply_markup)
            return
        
        wallet, chain = result
        tier, token_balance = await check_eligibility(wallet, chain)
        
        if tier == 0:
            keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.send_message(chat_id, LANGUAGES[lang]["no_assets"], reply_markup)
            return
        
        cursor.execute("REPLACE INTO eligible (user_id, wallet, chain, tier, verified, token_balance, social_tasks_completed) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (user_id, wallet, chain, tier, 1, token_balance, 1 if check_mandatory_tasks(user_id) else 0))
        conn.commit()
        
        context.user_data["format_args"] = {"tier": tier}
        keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.send_message(chat_id, LANGUAGES[lang]["verified"], reply_markup)

    async def calculate_airdrop(self, campaign_id: int, token_id: int):
        cursor.execute("SELECT total_tokens FROM campaigns WHERE id = ? AND active = 1", (campaign_id,))
        campaign = cursor.fetchone()
        if not campaign:
            return
        
        total_tokens = campaign[0]
        cursor.execute("SELECT user_id, tier FROM eligible WHERE verified = 1 AND social_tasks_completed = 1")
        eligible_users = cursor.fetchall()
        
        if not eligible_users:
            return
        
        total_weight = sum([user[1] for user in eligible_users])
        if total_weight == 0:
            return
        
        per_weight = total_tokens / total_weight
        
        for user_id, tier in eligible_users:
            cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            if result:
                wallet, chain = result
                cursor.execute("SELECT amount, contract_address FROM token_distributions WHERE token_id = ? AND tier = ?", (token_id, tier))
                token_data = cursor.fetchone()
                amount = token_data[0] if token_data else per_weight * tier
                cursor.execute("REPLACE INTO distributions (user_id, wallet, chain, amount, status) VALUES (?, ?, ?, ?, ?)",
                               (user_id, wallet, chain, amount, "pending"))
        conn.commit()

    async def calculate_airdrop_by_tier(self, campaign_id: int, token_id: int, tier: int):
        cursor.execute("SELECT total_tokens FROM campaigns WHERE id = ? AND active = 1", (campaign_id,))
        campaign = cursor.fetchone()
        if not campaign:
            return
        
        total_tokens = campaign[0]
        cursor.execute("SELECT user_id FROM eligible WHERE verified = 1 AND social_tasks_completed = 1 AND tier = ?", (tier,))
        eligible_users = [row[0] for row in cursor.fetchall()]
        
        if not eligible_users:
            return
        
        cursor.execute("SELECT amount, contract_address FROM token_distributions WHERE token_id = ? AND tier = ?", (token_id, tier))
        token_data = cursor.fetchone()
        amount_per_user = token_data[0] if token_data else total_tokens / len(eligible_users)
        
        for user_id in eligible_users:
            cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            if result:
                wallet, chain = result
                cursor.execute("REPLACE INTO distributions (user_id, wallet, chain, amount, status) VALUES (?, ?, ?, ?, ?)",
                               (user_id, wallet, chain, amount_per_user, "pending"))
        conn.commit()

    async def calculate_airdrop_all(self, campaign_id: int, token_id: str):
        cursor.execute("SELECT total_tokens FROM campaigns WHERE id = ? AND active = 1", (campaign_id,))
        campaign = cursor.fetchone()
        if not campaign:
            return
        
        total_tokens = campaign[0]
        cursor.execute("SELECT user_id FROM eligible WHERE verified = 1 AND social_tasks_completed = 1")
        eligible_users = [row[0] for row in cursor.fetchall()]
        
        if not eligible_users:
            return
        
        amount_per_user = total_tokens / len(eligible_users)
        
        for user_id in eligible_users:
            cursor.execute("SELECT wallet, chain FROM submissions WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            if result:
                wallet, chain = result
                cursor.execute("REPLACE INTO distributions (user_id, wallet, chain, amount, status) VALUES (?, ?, ?, ?, ?)",
                               (user_id, wallet, chain, amount_per_user, "pending"))
        conn.commit()

    async def distribute_tokens(self, chat_id: str, context: BotContext, token_id: int, lang: str):
        cursor.execute("SELECT user_id, wallet, chain, amount FROM distributions WHERE status = 'pending'")
        distributions = cursor.fetchall()
        
        for user_id, wallet, chain, amount in distributions:
            try:
                cursor.execute("SELECT tier FROM eligible WHERE user_id = ?", (user_id,))
                tier = cursor.fetchone()[0]
                cursor.execute("SELECT contract_address FROM token_distributions WHERE token_id = ? AND tier = ?", (token_id, tier))
                contract_address_result = cursor.fetchone()
                contract_address = contract_address_result[0] if contract_address_result else TOKEN_CONTRACT_ADDRESS
                
                if chain == "ETH":
                    tx_hash = await self.send_eth_tokens(wallet, amount, contract_address)
                elif chain == "BSC":
                    tx_hash = await self.send_bsc_tokens(wallet, amount, contract_address)
                elif chain == "SOL":
                    tx_hash = await self.send_sol_tokens(wallet, amount)
                elif chain == "XRP":
                    tx_hash = await self.send_xrp_tokens(wallet, amount)
                else:
                    continue
                
                cursor.execute("UPDATE distributions SET status = 'completed', tx_hash = ? WHERE user_id = ?", (tx_hash, user_id))
                conn.commit()
                
                context.user_data["format_args"] = {"amount": amount, "wallet": wallet, "tx_hash": tx_hash}
                await context.send_message(user_id, LANGUAGES[lang]["sent_tokens"])
                logger.info(f"Sent {amount} tokens to {wallet} on {chain}: {tx_hash}")
                
            except Exception as e:
                cursor.execute("UPDATE distributions SET status = 'failed' WHERE user_id = ?", (user_id,))
                conn.commit()
                context.user_data["format_args"] = {"amount": amount, "wallet": wallet, "error": str(e)}
                await context.send_message(user_id, LANGUAGES[lang]["failed_tokens"])
                logger.error(f"Failed to send {amount} to {wallet} on {chain}: {str(e)}")
                
        await context.send_message(chat_id, "Distribution process completed.")

    async def send_eth_tokens(self, to_address: str, amount: float, contract_address: str) -> str:
        token_contract = web3_eth.eth.contract(address=Web3.to_checksum_address(contract_address), abi=TOKEN_ABI)
        amount_wei = int(amount * 10**18)
        nonce = web3_eth.eth.get_transaction_count(ETH_SENDER_ADDRESS)
        tx = token_contract.functions.transfer(to_address, amount_wei).build_transaction({
            'from': ETH_SENDER_ADDRESS,
            'nonce': nonce,
            'gas': 200000,
            'gasPrice': web3_eth.to_wei('50', 'gwei')
        })
        signed_tx = web3_eth.eth.account.sign_transaction(tx, private_key=ETH_PRIVATE_KEY)
        tx_hash = web3_eth.eth.send_raw_transaction(signed_tx.rawTransaction)
        return web3_eth.to_hex(tx_hash)

    async def send_bsc_tokens(self, to_address: str, amount: float, contract_address: str) -> str:
        token_contract = web3_bsc.eth.contract(address=Web3.to_checksum_address(contract_address), abi=TOKEN_ABI)
        amount_wei = int(amount * 10**18)
        nonce = web3_bsc.eth.get_transaction_count(ETH_SENDER_ADDRESS)
        tx = token_contract.functions.transfer(to_address, amount_wei).build_transaction({
            'from': ETH_SENDER_ADDRESS,
            'nonce': nonce,
            'gas': 200000,
            'gasPrice': web3_bsc.to_wei('5', 'gwei')
        })
        signed_tx = web3_bsc.eth.account.sign_transaction(tx, private_key=ETH_PRIVATE_KEY)
        tx_hash = web3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction)
        return web3_bsc.to_hex(tx_hash)

    async def send_sol_tokens(self, to_address: str, amount: float) -> str:
        sender_keypair = Keypair.from_base58_string(SOL_SENDER_PRIVATE_KEY)
        receiver_pubkey = Pubkey.from_string(to_address)
        lamports = int(amount * 10**9)
        instruction = transfer(TransferParams(
            from_pubkey=sender_keypair.pubkey(),
            to_pubkey=receiver_pubkey,
            lamports=lamports
        ))
        message = Message([instruction], sender_keypair.pubkey())
        tx = Transaction.from_bytes(bytes(message))
        tx.sign(sender_keypair)
        response = rate_limited_request(SOL_RPC_URL, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [tx.serialize().hex()]
        })
        return response["result"]

    async def send_xrp_tokens(self, to_address: str, amount: float) -> str:
        wallet = Wallet(seed=XRP_SENDER_SEED, sequence=0)
        payment = Payment(
            account=XRP_SENDER_ADDRESS,
            destination=to_address,
            amount=xrp_to_drops(amount)
        )
        response = xrp_client.submit(payment, wallet)
        return response.result["tx_json"]["hash"]

async def get_leaderboard_text(lang: str) -> str:
    cursor.execute("SELECT user_id, Birdz_balance FROM users ORDER BY Birdz_balance DESC LIMIT 10")
    top_users = cursor.fetchall()
    if not top_users:
        return "No users in the leaderboard yet."
    
    leaderboard_lines = []
    for i, (user_id, balance) in enumerate(top_users, 1):
        cursor.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
        username = cursor.fetchone()[0]
        leaderboard_lines.append(f"{i}. {username} - {balance} Birdz Coins")
    
    return "🏆 *Leaderboard* 🏆\n\n" + "\n".join(leaderboard_lines)

async def view_admins(chat_id: str):
    cursor.execute("SELECT user_id, username, role FROM admins")
    admins = cursor.fetchall()
    if not admins:
        await context.send_message(chat_id, "No admins found.")
        return
    admin_list = "\n".join([f"ID: {a[0]}, Username: {a[1]}, Role: {a[2]}" for a in admins])
    await context.send_message(chat_id, f"Current Admins:\n{admin_list}")

# Discord and Telegram Integration
bot = AirdropBot()

# Telegram Handlers
async def telegram_start(update: Update, context):
    bot_context = BotContext("telegram")
    bot_context.bot = context.bot
    await bot.start(update, bot_context)

async def telegram_button(update: Update, context):
    bot_context = BotContext("telegram")
    bot_context.bot = context.bot
    await bot.button_handler(update, bot_context)

async def telegram_message(update: Update, context):
    bot_context = BotContext("telegram")
    bot_context.bot = context.bot
    await bot.handle_message(update, bot_context)

# Discord Bot Setup
class DiscordBot(discord_commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!Birdz ", intents=intents)

    async def on_ready(self):
        logger.info(f"Discord Bot logged in as {self.user}")

    async def on_message(self, message):
        if message.author == self.user:
            return
        bot_context = BotContext("discord")
        bot_context.bot = self
        if message.content.startswith("!Birdz"):
            parts = message.content.split()
            if len(parts) == 1:
                await bot.start(message, bot_context)
            else:
                update = message
                update.callback_query = type('obj', (object,), {'data': parts[1], 'from_user': message.author, 'message': message})
                await bot.button_handler(update, bot_context)
        else:
            await bot.handle_message(message, bot_context)

# Main Execution
if __name__ == "__main__":
    # Telegram Bot
    if TELEGRAM_TOKEN:
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        application.add_handler(CommandHandler("start", telegram_start))
        application.add_handler(CallbackQueryHandler(telegram_button))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_message))
        bot.telegram_app = application
        application.run_polling()

    # Discord Bot
    if DISCORD_TOKEN:
        discord_bot = DiscordBot()
        bot.discord_bot = discord_bot
        discord_bot.run(DISCORD_TOKEN)

    # Keep the script running if both are disabled
    if not TELEGRAM_TOKEN and not DISCORD_TOKEN:
        logger.error("No bot tokens provided. Please set TELEGRAM_TOKEN or DISCORD_TOKEN in .env")
        while True:
            asyncio.sleep(1)
