import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import random
import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import make_interp_spline
import io
import os
import sqlite3
from datetime import datetime, timedelta
import webserver

# Database setup
def setup_database():
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS currencies
                 (symbol TEXT PRIMARY KEY, rate REAL, volatility REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS rate_history
                 (symbol TEXT, rate REAL, timestamp TEXT)''')
    
    # Check if name column exists
    cursor = c.execute('PRAGMA table_info(currencies)')
    columns = [col[1] for col in cursor.fetchall()]
    if 'name' not in columns:
        c.execute("ALTER TABLE currencies ADD COLUMN name TEXT")

    conn.commit()
    conn.close()

def init_default_currencies():
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM currencies")
    if c.fetchone()[0] == 0:
        default_currencies = [
            ('SOL', 1.0, 0.01, 'Solar'),
            ('LUN', 3.5, 0.02, 'Luna'),
            ('TAR', 0.8, 0.04, 'Taro'),
            ('VEX', 5.2, 0.1, 'Vexus')
        ]
        c.executemany("INSERT INTO currencies (symbol, rate, volatility, name) VALUES (?, ?, ?, ?)", default_currencies)
        for currency in default_currencies:
            c.execute("INSERT INTO rate_history VALUES (?, ?, ?)",
                     (currency[0], currency[1], datetime.now().isoformat()))
    conn.commit()
    conn.close()

setup_database()
init_default_currencies()

# Интенты
intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='/', intents=intents)

# Валюты
BASE_CURRENCY = 'SOL'

def get_exchange_rates():
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("SELECT symbol, rate FROM currencies")
    rates = dict(c.fetchall())
    conn.close()
    return rates

def get_volatilities():
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("SELECT symbol, volatility FROM currencies")
    volatilities = dict(c.fetchall())
    conn.close()
    return volatilities

def get_rate_history():
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    c.execute("""
        SELECT symbol, rate FROM rate_history 
        WHERE timestamp > ? 
        ORDER BY timestamp DESC
    """, (week_ago,))
    history = {}
    for symbol, rate in c.fetchall():
        if symbol not in history:
            history[symbol] = []
        history[symbol].append(rate)
    conn.close()
    return history

exchange_rates = get_exchange_rates()
currency_volatility = get_volatilities()
exchange_rate_history = get_rate_history()

# Переменные для канала и роли
exchange_rate_channel_id = None
currency_manager_role_id = None


# Проверка доступа по роли
def has_currency_manager_role():

    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        if currency_manager_role_id is None:
            return False
        role = discord.utils.get(ctx.author.roles, id=currency_manager_role_id)
        return role is not None

    return commands.check(predicate)


# Функция обновления курсов валют
def update_exchange_rates():
    for currency in exchange_rates:
        if currency == BASE_CURRENCY:
            continue
        volatility = currency_volatility.get(currency, 0.05)
        change = random.uniform(-volatility, volatility)
        exchange_rates[currency] *= (1 + change)
        exchange_rates[currency] = round(exchange_rates[currency], 4)

        conn = sqlite3.connect('currency.db')
        c = conn.cursor()
        c.execute("UPDATE currencies SET rate = ? WHERE symbol = ?", (exchange_rates[currency], currency))
        c.execute("INSERT INTO rate_history VALUES (?, ?, ?)", (currency, exchange_rates[currency], datetime.now().isoformat()))
        conn.commit()
        conn.close()


        exchange_rate_history[currency].append(exchange_rates[currency])
        if len(exchange_rate_history[currency]) > 7:
            exchange_rate_history[currency].pop(0)


# Текст курсов валют
def get_exchange_rate_text():
    lines = [f'Курс {BASE_CURRENCY} на сегодня:']
    for currency, rate in exchange_rates.items():
        if currency != BASE_CURRENCY:
            lines.append(f'1 {BASE_CURRENCY} = {rate} {currency}')
    return '\n'.join(lines)


# Генерация графика
def generate_exchange_chart(currency_from, currency_to):
    rates_from = exchange_rate_history[currency_from]
    rates_to = exchange_rate_history[currency_to]
    rates = [b / a for a, b in zip(rates_from, rates_to)]

    days = list(range(len(rates)))
    day_labels = [
        f"{7 - i} дн. назад" if i < 7 else "сегодня" for i in reversed(days)
    ]

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
    plt.title(f'{currency_from}/{currency_to} за последнюю неделю',
              fontsize=14)
    plt.xlabel('День', fontsize=12)
    plt.ylabel(f'Курс {currency_from} к {currency_to}', fontsize=12)
    plt.grid(True, linestyle='--', alpha=0.6)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf


# --- Slash-команды ---


@bot.slash_command(name="exchangerate",
                   description="Узнать курс и график валют")
async def exchangerate(ctx,
                       currency_from: str,
                       currency_to: str,
                       amount: float = 1.0):
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


@bot.slash_command(name="setrate",
                   description="Изменить курс валюты и её волатильность")
@has_currency_manager_role()
async def setrate(ctx,
                  currency: str,
                  new_rate: float,
                  new_volatility: float = None):
    currency = currency.upper()

    if currency not in exchange_rates:
        await ctx.respond("Нет такой валюты.")
        return

    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("UPDATE currencies SET rate = ? WHERE symbol = ?", (new_rate, currency))
    c.execute("INSERT INTO rate_history VALUES (?, ?, ?)", (currency, new_rate, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    exchange_rates[currency] = round(new_rate, 4)
    exchange_rate_history[currency][-1] = round(new_rate,4)

    response = f"Курс {currency} установлен на {new_rate}."

    if new_volatility is not None:
        conn = sqlite3.connect('currency.db')
        c = conn.cursor()
        c.execute("UPDATE currencies SET volatility = ? WHERE symbol = ?", (new_volatility, currency))
        conn.commit()
        conn.close()
        currency_volatility[currency] = new_volatility
        response += f" Волатильность установлена на {new_volatility}."

    await ctx.respond(response)


@bot.slash_command(name="addcurrency", description="Добавить валюту")
@has_currency_manager_role()
async def addcurrency(ctx,
                     currency: str,
                     rate: float,
                     volatility: float = 0.05,
                     name: str = "Без названия"):
    currency = currency.upper()

    if currency in exchange_rates:
        await ctx.respond("Эта валюта уже существует.")
        return

    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("INSERT INTO currencies (symbol, rate, volatility, name) VALUES (?, ?, ?, ?)",
             (currency, rate, volatility, name))
    c.execute("INSERT INTO rate_history VALUES (?, ?, ?)",
             (currency, rate, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    exchange_rates[currency] = rate
    currency_volatility[currency] = volatility
    exchange_rate_history[currency] = [rate]
    await ctx.respond(f"Валюта {currency} ({name}) добавлена.")


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

    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("DELETE FROM currencies WHERE symbol = ?", (currency,))
    c.execute("DELETE FROM rate_history WHERE symbol = ?", (currency,))
    conn.commit()
    conn.close()

    del exchange_rates[currency]
    del currency_volatility[currency]
    del exchange_rate_history[currency]
    await ctx.respond(f"Валюта {currency} удалена.")


@bot.slash_command(name="setexchangechannel",
                   description="Назначить канал для публикации курсов валют")
@commands.has_permissions(administrator=True)
async def setexchangechannel(ctx, channel: discord.TextChannel):
    global exchange_rate_channel_id
    exchange_rate_channel_id = channel.id
    await ctx.respond(
        f"Канал {channel.mention} теперь выбран для публикации курсов валют.")


@bot.slash_command(name="setcurrencyrole",
                   description="Назначить роль для управления валютами")
@commands.has_permissions(administrator=True)
async def setcurrencyrole(ctx, role: discord.Role):
    global currency_manager_role_id
    currency_manager_role_id = role.id
    await ctx.respond(f"Роль {role.mention} теперь может управлять валютами.")


@bot.slash_command(name="currencylist", description="Показать список всех валют, их курсы и волатильность")
async def currencylist(ctx):
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("SELECT symbol, rate, volatility, COALESCE(name, '') FROM currencies")
    currencies = c.fetchall()
    conn.close()

    # Формируем красивое сообщение
    message = "**Список всех валют:**\n\n"
    message += f"**Основная валюта: {BASE_CURRENCY}**\n\n"

    for symbol, rate, volatility, name in currencies:
        name_text = f" — {name}" if name else ""
        if symbol == BASE_CURRENCY:
            message += f"🔸 **{symbol}** (основная валюта{name_text})\n"
        else:
            exchange_rate = rate / exchange_rates[BASE_CURRENCY]
            message += f"💠 **{symbol}**{name_text}\n"
            message += f"  • Курс: 1 {BASE_CURRENCY} = {round(exchange_rate, 4)} {symbol}\n"
            message += f"  • Волатильность: {round(volatility * 100, 2)}%\n\n"

    await ctx.respond(message)





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
        exchange_rate_history[currency] = [
            rate / old_base_rate for rate in exchange_rate_history[currency]
        ]

    BASE_CURRENCY = new_base_currency

    await ctx.respond(f"Основная валюта изменена на {BASE_CURRENCY}!")


@bot.slash_command(name="help", description="Показать список команд бота")
async def help_command(ctx):
    help_text = (
        "**Список команд:**\n"
        "/exchangerate валюта1 валюта2 [количество] — Показать курс и график валют\n"
        "/currencylist — Показать список всех валют, их курсы и волатильность\n"
        "/setrate валюта новый_курс [новая_волатильность] — Изменить курс валюты и её волатильность (*админская команда*)\n"
        "/addcurrency валюта курс [волатильность] — Добавить новую валюту (*админская команда*)\n"
        "/removecurrency валюта — Удалить валюту (*админская команда*)\n"
        "/setexchangechannel #канал — Назначить канал для публикации курсов валют (*админская команда*)\n"
        "/setcurrencyrole @роль — Назначить роль для управления валютами (*админская команда*)\n"
        "/maincurrency валюта — Изменить основную валюту (*админская команда*)\n"
    )
    await ctx.respond(help_text)


# Планировщик обновления курсов
scheduler = AsyncIOScheduler()


@scheduler.scheduled_job('cron', hour=12)
async def scheduled_exchange_rate_post():
    if exchange_rate_channel_id is None:
        return
    channel = bot.get_channel(exchange_rate_channel_id)
    if channel:
        update_exchange_rates()
        text = get_exchange_rate_text()
        await channel.send(text)


# Когда бот готов
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    scheduler.start()


# Запуск бота
webserver.keep_alive()
token = os.getenv("DISCORD_TOKEN")  # Discord bot token from Replit Secrets
bot.run(token)