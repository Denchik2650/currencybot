import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import random
import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import make_interp_spline
import sqlite3
import io

# --- Настройка базы данных ---
conn = sqlite3.connect('currencies.db')
c = conn.cursor()
c.execute('''
CREATE TABLE IF NOT EXISTS currencies (
    name TEXT PRIMARY KEY,
    rate REAL NOT NULL,
    volatility REAL NOT NULL
)
''')
c.execute('''
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
''')
conn.commit()

# --- Работа с базой данных ---
def load_currencies():
    c.execute("SELECT name, rate, volatility FROM currencies")
    rows = c.fetchall()
    rates = {}
    volatility = {}
    for name, rate, vol in rows:
        rates[name] = rate
        volatility[name] = vol
    return rates, volatility

def save_currency(name, rate, volatility):
    c.execute("INSERT OR REPLACE INTO currencies (name, rate, volatility) VALUES (?, ?, ?)", (name, rate, volatility))
    conn.commit()

def delete_currency(name):
    c.execute("DELETE FROM currencies WHERE name = ?", (name,))
    conn.commit()

def load_setting(key):
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    return row[0] if row else None

def save_setting(key, value):
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

# --- Инициализация ---
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix='/', intents=intents)

# Переменные
BASE_CURRENCY = load_setting('base_currency') or 'SOL'
exchange_rates, currency_volatility = load_currencies()
if BASE_CURRENCY not in exchange_rates:
    exchange_rates[BASE_CURRENCY] = 1.0
    currency_volatility[BASE_CURRENCY] = 0.01
    save_currency(BASE_CURRENCY, 1.0, 0.01)

exchange_rate_history = {currency: [rate] for currency, rate in exchange_rates.items()}
exchange_rate_channel_id = int(load_setting('exchange_channel') or 0)
currency_manager_role_id = int(load_setting('currency_role') or 0)

# Проверка доступа
def has_currency_manager_role():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        if currency_manager_role_id == 0:
            return False
        role = discord.utils.get(ctx.author.roles, id=currency_manager_role_id)
        return role is not None
    return commands.check(predicate)

# --- Логика валют ---
def update_exchange_rates():
    for currency in exchange_rates:
        if currency == BASE_CURRENCY:
            continue
        volatility = currency_volatility.get(currency, 0.05)
        change = random.uniform(-volatility, volatility)
        exchange_rates[currency] *= (1 + change)
        exchange_rates[currency] = round(exchange_rates[currency], 4)

        save_currency(currency, exchange_rates[currency], volatility)

        if currency in exchange_rate_history:
            exchange_rate_history[currency].append(exchange_rates[currency])
            if len(exchange_rate_history[currency]) > 7:
                exchange_rate_history[currency].pop(0)
        else:
            exchange_rate_history[currency] = [exchange_rates[currency]]

def get_exchange_rate_text():
    lines = [f'Курс {BASE_CURRENCY} на сегодня:']
    for currency, rate in exchange_rates.items():
        if currency != BASE_CURRENCY:
            lines.append(f'1 {BASE_CURRENCY} = {rate} {currency}')
    return '\n'.join(lines)

def generate_exchange_chart(currency_from, currency_to):
    rates_from = exchange_rate_history.get(currency_from, [1.0])
    rates_to = exchange_rate_history.get(currency_to, [1.0])

    rates = [b / a for a, b in zip(rates_from, rates_to)]

    days = list(range(len(rates)))
    day_labels = [f"{7 - i} дн. назад" if i < 7 else "сегодня" for i in reversed(days)]

    x = np.array(days)
    y = np.array(rates)
    if len(x) >= 3:
        x_smooth = np.linspace(x.min(), x.max(), 300)
        spline = make_interp_spline(x, y, k=2)
        y_smooth = spline(x_smooth)
    else:
        x_smooth = x
        y_smooth = y

    plt.figure(figsize=(7, 5))
    plt.plot(x_smooth, y_smooth, color='blue')
    plt.scatter(x, y, color='red')
    plt.xticks(ticks=days, labels=day_labels, rotation=45)
    plt.title(f'{currency_from}/{currency_to} за последнюю неделю', fontsize=14)
    plt.xlabel('День', fontsize=12)
    plt.ylabel(f'Курс {currency_from} к {currency_to}', fontsize=12)
    plt.grid(True, linestyle='--', alpha=0.6)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

# --- Команды ---
@bot.slash_command(name="exchangerate", description="Узнать курс и график валют")
async def exchangerate(ctx, currency_from: str, currency_to: str, amount: float = 1.0):
    currency_from = currency_from.upper()
    currency_to = currency_to.upper()

    if currency_from not in exchange_rates or currency_to not in exchange_rates:
        await ctx.respond("Неверная валюта.")
        return

    rate = exchange_rates[currency_to] / exchange_rates[currency_from]
    converted_amount = round(amount * rate, 4)
    rate = round(rate, 4)

    text = f"{amount} {currency_from} = {converted_amount} {currency_to}\n(1 {currency_from} = {rate} {currency_to})"

    chart = generate_exchange_chart(currency_from, currency_to)
    file = discord.File(chart, filename="chart.png")
    await ctx.respond(text, file=file)

@bot.slash_command(name="setrate", description="Изменить курс валюты и её волатильность")
@has_currency_manager_role()
async def setrate(ctx, currency: str, new_rate: float, new_volatility: float = None):
    currency = currency.upper()

    if currency not in exchange_rates:
        await ctx.respond("Нет такой валюты.")
        return

    exchange_rates[currency] = round(new_rate, 4)
    if new_volatility is not None:
        currency_volatility[currency] = new_volatility

    save_currency(currency, exchange_rates[currency], currency_volatility[currency])
    await ctx.respond(f"Курс {currency} установлен на {new_rate}.\nВолатильность: {currency_volatility[currency]}")

@bot.slash_command(name="addcurrency", description="Добавить валюту")
@has_currency_manager_role()
async def addcurrency(ctx, currency: str, rate: float, volatility: float = 0.05):
    currency = currency.upper()

    if currency in exchange_rates:
        await ctx.respond("Эта валюта уже существует.")
        return

    exchange_rates[currency] = round(rate, 4)
    currency_volatility[currency] = volatility
    exchange_rate_history[currency] = [rate]
    save_currency(currency, rate, volatility)
    await ctx.respond(f"Валюта {currency} добавлена.")

@bot.slash_command(name="removecurrency", description="Удалить валюту")
@has_currency_manager_role()
async def removecurrency(ctx, currency: str):
    currency = currency.upper()

    if currency == BASE_CURRENCY:
        await ctx.respond("Нельзя удалить базовую валюту.")
        return

    if currency not in exchange_rates:
        await ctx.respond("Валюта не найдена.")
        return

    del exchange_rates[currency]
    del currency_volatility[currency]
    if currency in exchange_rate_history:
        del exchange_rate_history[currency]
    delete_currency(currency)
    await ctx.respond(f"Валюта {currency} удалена.")

@bot.slash_command(name="setexchangechannel", description="Назначить канал для публикации курсов валют")
@commands.has_permissions(administrator=True)
async def setexchangechannel(ctx, channel: discord.TextChannel):
    global exchange_rate_channel_id
    exchange_rate_channel_id = channel.id
    save_setting('exchange_channel', str(channel.id))
    await ctx.respond(f"Канал {channel.mention} выбран для публикации курсов валют.")

@bot.slash_command(name="setcurrencyrole", description="Назначить роль для управления валютами")
@commands.has_permissions(administrator=True)
async def setcurrencyrole(ctx, role: discord.Role):
    global currency_manager_role_id
    currency_manager_role_id = role.id
    save_setting('currency_role', str(role.id))
    await ctx.respond(f"Роль {role.mention} теперь управляет валютами.")

@bot.slash_command(name="maincurrency", description="Изменить основную валюту")
@commands.has_permissions(administrator=True)
async def maincurrency(ctx, new_base_currency: str):
    global BASE_CURRENCY
    new_base_currency = new_base_currency.upper()

    if new_base_currency not in exchange_rates:
        await ctx.respond("Такой валюты нет в списке.")
        return

    old_base_rate = exchange_rates[new_base_currency]

    for currency in exchange_rates:
        exchange_rates[currency] /= old_base_rate
        exchange_rate_history[currency] = [rate / old_base_rate for rate in exchange_rate_history.get(currency, [1.0])]
        save_currency(currency, exchange_rates[currency], currency_volatility[currency])

    BASE_CURRENCY = new_base_currency
    save_setting('base_currency', BASE_CURRENCY)

    await ctx.respond(f"Основная валюта изменена на {BASE_CURRENCY}!")

@bot.slash_command(name="help", description="Показать список команд бота")
async def help_command(ctx):
    help_text = (
        "**Список команд:**\n"
        "/exchangerate валюта1 валюта2 [количество] — Показать курс и график валют\n"
        "/setrate валюта новый_курс [новая_волатильность] — Изменить курс валюты (*админская команда*)\n"
        "/addcurrency валюта курс [волатильность] — Добавить новую валюту (*админская команда*)\n"
        "/removecurrency валюта — Удалить валюту (*админская команда*)\n"
        "/setexchangechannel #канал — Назначить канал для курсов (*админская команда*)\n"
        "/setcurrencyrole @роль — Назначить роль для управления валютами (*админская команда*)\n"
        "/maincurrency валюта — Изменить основную валюту (*админская команда*)\n"
    )
    await ctx.respond(help_text)

# --- Автопост ---
scheduler = AsyncIOScheduler()

@scheduler.scheduled_job('cron', hour=12)
async def scheduled_exchange_rate_post():
    if not exchange_rate_channel_id:
        return
    channel = bot.get_channel(exchange_rate_channel_id)
    if channel:
        update_exchange_rates()
        text = get_exchange_rate_text()
        await channel.send(text)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    scheduler.start()

bot.run('YOUR_BOT_TOKEN')