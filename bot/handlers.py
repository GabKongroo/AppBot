# handlers.py
from telegram.ext import CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters
from callbacks import (
    start_command,
    genre_selected,
    mood_selected,
    handle_beat_navigation,
    handle_filter_selection,
    handle_bundle_navigation,
    GENRE, MOOD, BEAT_SELECTION,
    CATEGORY, category_selected,
    BUNDLE_SELECTION,
    handle_wrong_input  # <--- aggiungi questa importazione
)

conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start_command)],
    states={
        CATEGORY: [CallbackQueryHandler(category_selected)],
        GENRE: [CallbackQueryHandler(genre_selected)],
        MOOD: [CallbackQueryHandler(mood_selected)],
        BEAT_SELECTION: [
            CallbackQueryHandler(handle_beat_navigation, pattern="^(prev|next|preview|buy|menu|disabled_prev|disabled_next|change_filters|remove_all_filters)$"),
            CallbackQueryHandler(
                handle_filter_selection,
                pattern="^(filter_genre|filter_mood|filter_price|back_to_filters|select_genre_.*|select_mood_.*|select_price_.*|remove_genre|remove_mood|remove_price|apply_filters|cancel_filters|disabled_.*|change_filters)$"
            ),
        ],
        BUNDLE_SELECTION: [
            CallbackQueryHandler(handle_bundle_navigation, pattern="^(bundle_prev|bundle_next|bundle_preview|bundle_buy|menu)$"),
        ],
    },
    fallbacks=[
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_wrong_input),
    ],
    allow_reentry=True
)