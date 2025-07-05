# handlers.py
from telegram.ext import CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters
from callbacks import (
    start_command,
    genre_selected,
    mood_selected,
    handle_beat_navigation,
    handle_filter_selection,
    GENRE, MOOD, BEAT_SELECTION,
    CATEGORY, category_selected,
    handle_wrong_input  # <--- aggiungi questa importazione
)

conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start_command)],
    states={
        CATEGORY: [CallbackQueryHandler(category_selected)],
        GENRE: [CallbackQueryHandler(genre_selected)],
        MOOD: [CallbackQueryHandler(mood_selected)],
        BEAT_SELECTION: [
            CallbackQueryHandler(handle_beat_navigation, pattern="^(prev|next|preview|buy|menu|disabled_prev|disabled_next|change_filters)$"),
            CallbackQueryHandler(
                handle_filter_selection,
                pattern="^(filter_select_genre|filter_select_mood|filter_select_price|filter_back|set_genre_.*|set_mood_.*|set_price_.*|disabled_.*|change_filters)$"
            ),
        ],
    },
    fallbacks=[
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_wrong_input),
    ],
    allow_reentry=True
)