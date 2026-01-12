import discord
from discord.ext import commands
import anthropic
import os

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Initialize Anthropic client
anthropic_client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY")
)

# PuddleJumper knowledge base
SYSTEM_PROMPT = """You are a helpful support assistant for PuddleJumper, a rideshare driver decision-making app.

KEY INFORMATION:
- PuddleJumper helps drivers decide which ride offers to accept
- Core formula: ACCEPT if true_net_rpm >= (custom_cpm × opportunity_multiplier)
- true_net_rpm = fare / (pickup_miles + trip_miles)
- Drivers set custom CPM (cost per mile) per market
- Green zones = profitable areas (bonus)
- Red zones = areas to avoid (penalty)

FEATURES:
1. Market Manager - Add cities, set CPM, define zones
2. Decision Engine - Auto-evaluates ride offers
3. H3 Geospatial System - Hexagonal zones for location intelligence
4. Monitoring Service - Watches for new offers

Help drivers with: setup, CPM optimization, zone configuration, troubleshooting.
Be friendly, concise, and practical."""

SUPPORT_CHANNEL_NAME = "app-support"

@bot.event
async def on_ready():
    print(f'✅ {bot.user.name} is online!')
    print(f'Connected to {len(bot.guilds)} server(s)')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    should_respond = (
        (message.channel.name == SUPPORT_CHANNEL_NAME if hasattr(message.channel, 'name') else False) or
        isinstance(message.channel, discord.DMChannel) or
        bot.user in message.mentions
    )
    
    if should_respond:
        async with message.channel.typing():
            try:
                response = anthropic_client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": message.content}]
                )
                
                reply = response.content[0].text
                
                if len(reply) > 2000:
                    chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
                    for chunk in chunks:
                        await message.channel.send(chunk)
                else:
                    await message.channel.send(reply)
                    
            except Exception as e:
                await message.channel.send(f"❌ Error: {str(e)}")
                print(f"Error: {e}")
    
    await bot.process_commands(message)

@bot.command(name='info')
async def info_command(ctx):
    embed = discord.Embed(
        title="🤖 PuddleJumper Support Bot",
        description="I'm here to help you with PuddleJumper!",
        color=0x5865F2
    )
    embed.add_field(
        name="💬 How to use:",
        value="Mention me in #app-support or DM me!",
        inline=False
    )
    embed.add_field(
        name="📚 I can help with:",
        value="• Markets & CPM\n• Red/green zones\n• Decision engine\n• Troubleshooting",
        inline=False
    )
    embed.add_field(
        name="⚡ Quick commands:",
        value="`!ask <question>` - Ask me anything\n`!info` - Show this message",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name='ask')
async def ask_command(ctx, *, question):
    async with ctx.typing():
        try:
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": question}]
            )
            await ctx.send(response.content[0].text)
        except Exception as e:
            await ctx.send(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN not found!")
        exit(1)
    
    print("🚀 Starting PuddleJumper Discord Bot...")
    bot.run(token)
