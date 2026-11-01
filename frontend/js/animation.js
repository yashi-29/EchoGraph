/* ============================================================
   ANIMATION.JS — the twin-thread motif and ambient particles.
   (The knowledge graph renderer lives in graph.js.)
   ============================================================ */

const DTAnim = (() => {

  /* ---------- build the twin-thread svg into any container ---------- */
  function mountTwinThread(selector){
    const el = document.querySelector(selector);
    if(!el) return;
    el.innerHTML = `
      <svg viewBox="0 0 220 220">
        <defs>
          <linearGradient id="threadGradient" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stop-color="#FF8B5E"/>
            <stop offset="100%" stop-color="#6EE7C0"/>
          </linearGradient>
        </defs>
        <path class="thread-line" d="M 54 110 Q 110 60 166 110" />
        <circle class="orb-you" cx="54" cy="110" r="20"/>
        <circle class="orb-twin" cx="166" cy="110" r="20"/>
      </svg>`;
  }

  /* ---------- ambient drifting particle field (hero / building bg) ---------- */
  function mountAmbientField(selector, count = 18){
    const el = document.querySelector(selector);
    if(!el) return;
    const frag = document.createDocumentFragment();
    for(let i=0; i<count; i++){
      const dot = document.createElement('span');
      dot.style.left = Math.random()*100 + '%';
      dot.style.top = Math.random()*100 + '%';
      dot.style.animationDuration = (4 + Math.random()*5) + 's';
      dot.style.animationDelay = (Math.random()*4) + 's';
      dot.style.opacity = (0.2 + Math.random()*0.5).toFixed(2);
      frag.appendChild(dot);
    }
    el.appendChild(frag);
  }

  return { mountTwinThread, mountAmbientField };
})();
