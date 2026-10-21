"""Settings › Cards: detected GPUs, card records and edit reviews."""

PAGE = "settings"
NAME = "cards"
TITLE = "Cards"
ORDER = 3


def html(_status):
    return '''<div id="cards-page">
<p id="cards-error" role="alert" tabindex="-1" hidden></p>
<div id="cards-detection" aria-live="polite"></div>
<div id="cards-list" aria-live="polite"></div>
<button id="cards-add" type="button">Add card</button>
<form id="cards-form" hidden>
<h4 id="cards-form-title">Add card</h4>
<p id="cards-form-error" role="alert" tabindex="-1" hidden></p>
<label>Card ID <input name="card_id" required pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}"></label>
<label>Name <input name="name" required></label>
<label>GPU <select name="gpu_identity" required></select></label>
<label>VRAM ceiling (bytes) <input name="vram_ceiling_bytes" type="number" min="1" required></label>
<label>Default GPU layers <select name="default_gpu_layers"><option value="all">all</option><option value="number">Set a number</option></select></label>
<label id="cards-layer-count-label" hidden>GPU layers <input name="layer_count" type="number" min="1"></label>
<label>Smoke output tokens <input name="smoke_output_tokens" type="number" min="1" required></label>
<label>Bench prompt tokens <input name="bench_prompt_tokens" type="number" min="1" required></label>
<label>Bench generation tokens <input name="bench_generation_tokens" type="number" min="1" required></label>
<label>Bench repetitions <input name="bench_repetitions" type="number" min="1" required></label>
<label>Notes <textarea name="notes"></textarea></label>
<div class="profile-actions"><button type="submit" id="cards-save">Add card</button><button type="button" id="cards-cancel">Cancel</button></div>
</form>
</div>
<link rel="stylesheet" href="/cards.css">
<script src="/cards.js" defer></script>'''
