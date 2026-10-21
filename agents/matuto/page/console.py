"""The model console, available only during healthy agentic serving."""

PAGE = "console"
NAME = "console"
TITLE = "Console"
ORDER = 0
TABBED = False

CONSOLE_URL = "http://matuto.localhost:8889/"


def html(status):
    nonce = (status or {}).get("csrf", "")
    return f'''<p id="console-status" role="status" aria-live="polite">Checking serving state.</p>
<div id="console-available" hidden>
<p>One model is loaded at a time. Loading one here replaces the model a consumer request loaded, and the next request can replace yours. Download and delete are refused here; use <a href="#/model-files">Model files</a> to download models, and your external file manager to delete them.</p>
<iframe class="console-frame" id="model-console-frame" title="llama.cpp model console" sandbox="allow-downloads allow-forms allow-same-origin allow-scripts" referrerpolicy="no-referrer"></iframe>
<p>If the embedded console is blocked by the browser, <a id="console-separate" target="_blank" rel="noopener noreferrer" aria-disabled="true" tabindex="-1">open Model console separately</a>.</p>
</div>
<div id="console-unavailable" hidden></div>
<button type="button" id="console-refresh">Check serving state again</button>
<style nonce="{nonce}">.console-frame{{display:block;width:100%;min-height:min(70vh,52rem);border:1px solid var(--line);border-radius:.9rem;background:var(--panel)}}#console-available a,#console-unavailable a{{color:var(--accent)}}</style>
<script nonce="{nonce}">
'use strict';
(()=>{{
const status=document.getElementById('console-status');
const available=document.getElementById('console-available');
const unavailable=document.getElementById('console-unavailable');
const frame=document.getElementById('model-console-frame');
const separate=document.getElementById('console-separate');
const refresh=document.getElementById('console-refresh');
const PAGE=document.getElementById('page-console');
const URL={CONSOLE_URL!r};
function link(label,href){{const a=document.createElement('a');a.href=href;a.textContent=label;return a}}
function deactivate(){{
 if(frame.hasAttribute('src'))frame.removeAttribute('src');
 separate.removeAttribute('href');separate.setAttribute('aria-disabled','true');separate.tabIndex=-1;
 available.hidden=true;
}}
function render(data){{
 if(PAGE.hidden){{deactivate();return}}
 if(window.matutoApplyStatus)window.matutoApplyStatus(data);
 status.textContent='Serving state checked.';
 const usable=data.console&&data.console.usable;
 available.hidden=!usable;unavailable.hidden=usable;
 if(usable){{
   status.textContent='Console ready while healthy agentic serving is active.';
   if(!frame.hasAttribute('src'))frame.setAttribute('src',URL);
   separate.setAttribute('href',URL);separate.removeAttribute('aria-disabled');separate.tabIndex=0;
   unavailable.textContent='';return;
 }}
 deactivate();
 const state=data.state, reason=data.reason||'';
 unavailable.textContent='';
 const message=document.createElement('p');
 let recovery;
 if(state==='stopped'){{
   message.textContent='Agentic serving is not running'+(reason?': '+reason:'.');
   recovery=link('Start agentic serving on Overview','#overview');
 }}else if(state==='agentic'){{
   message.textContent='Agentic serving is unhealthy'+(reason?': '+reason:'.');
   recovery=link('Restart on Overview','#overview');
 }}else if(data.console&&data.console.transition){{
   message.textContent='Serving is starting or stopping. Wait until it settles.';
 }}else if(state==='voice'){{
   message.textContent='Voice serving is active. Loading a model here would unload the voice model.';
   recovery=link('Stop serving or Switch to agentic serving on Overview','#overview');
 }}else{{
   message.textContent='Serving state is unknown'+(reason?': '+reason:'.');
 }}
 unavailable.append(message);
 if(recovery)unavailable.append(recovery);
}}
let checkSequence=0;
async function check(){{
 const current=++checkSequence;
 if(PAGE.hidden){{deactivate();return}}
 try{{
   const response=await fetch('/api/console',{{cache:'no-store'}});if(!response.ok)throw new Error();
   const data=await response.json();if(current!==checkSequence||PAGE.hidden)return;render(data);
 }}catch(error){{
   if(current!==checkSequence||PAGE.hidden)return;
   deactivate();unavailable.hidden=false;unavailable.textContent='Serving state is unknown because Matuto is not answering. Check again when the manager responds.';
   status.textContent='Could not read serving state.';
 }}
}}
refresh.addEventListener('click',check);
window.addEventListener('hashchange',()=>setTimeout(()=>{{if(PAGE.hidden)deactivate();else check()}},0));
function poll(){{check().finally(()=>setTimeout(poll,2000))}}
document.addEventListener('DOMContentLoaded',poll,{{once:true}});
}})();
</script>'''
