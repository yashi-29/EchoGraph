/* ============================================================
   GRAPH.JS — knowledge graph page (graph.html)
   Fetches /api/v1/graph/ (people, organizations, topics built from the user's memories)
   and renders it as a force-directed canvas graph with a detail panel.
   ============================================================ */

const DTGraph = (() => {
  const API_BASE = window.DT_API_BASE || 'http://localhost:8000/api/v1';

  const TYPE_META = {
    you:          { label: 'You',          color: '#6EE7C0' },
    person:       { label: 'People',       single: 'Person',       color: '#FF8B5E' },
    organization: { label: 'Organizations', single: 'Organization', color: '#7AA2F7' },
    topic:        { label: 'Topics',       single: 'Topic',        color: '#B69CF0' }
  };
  const SOURCE_LABELS = {
    gmail: 'Gmail', google_calendar: 'Google Calendar', document: 'Documents', image: 'Images',
    voice_note: 'Voice notes', bookmark: 'Bookmarks'
  };

  const esc = (str) => {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
  };
  const sourceLabel = (s) => SOURCE_LABELS[s] || s;
  const plural = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
  function shortDate(iso){
    const d = new Date(iso);
    const opts = { month: 'short', day: 'numeric' };
    if(d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString([], opts);
  }
  function timeAgo(iso){
    const minutes = Math.round((Date.now() - new Date(iso)) / 60000);
    if(minutes < 1) return 'just now';
    if(minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if(hours < 24) return `${hours}h ago`;
    return shortDate(iso);
  }

  /* ---------------- renderer ---------------- */
  function createRenderer(canvas, graph, onSelect){
    const ctx = canvas.getContext('2d');
    const maxCount = Math.max(1, ...graph.nodes.filter(n => n.type !== 'you').map(n => n.count));
    const maxWeight = Math.max(1, ...graph.edges.map(e => e.weight));
    const nodes = graph.nodes.map(n => ({ ...n }));
    const byId = new Map(nodes.map(n => [n.id, n]));
    const edges = graph.edges
      .map(e => ({ ...e, a: byId.get(e.source), b: byId.get(e.target) }))
      .filter(e => e.a && e.b);
    const neighbors = new Map(nodes.map(n => [n.id, new Set()]));
    edges.forEach(e => { neighbors.get(e.a.id).add(e.b.id); neighbors.get(e.b.id).add(e.a.id); });

    let width = 0, height = 0;
    let hovered = null, selected = null, dragging = null;
    let alpha = 1, raf = null;
    const showAllLabels = nodes.length <= 22;
    const compact = () => width < 600; // phones: fewer labels, more spacing

    nodes.forEach(n => {
      n.r = n.type === 'you' ? 26 : 8 + 14 * Math.sqrt(n.count / maxCount);
    });

    function resize(){
      const rect = canvas.parentElement.getBoundingClientRect();
      const first = !width;
      width = rect.width; height = rect.height;
      canvas.width = width * devicePixelRatio;
      canvas.height = height * devicePixelRatio;
      canvas.style.width = width + 'px';
      canvas.style.height = height + 'px';
      ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
      if(first) seed();
      reheat();
    }

    // Deterministic start: topics on an inner ring, people/organizations on an outer one.
    function seed(){
      const cx = width / 2, cy = height / 2;
      const inner = nodes.filter(n => n.type === 'topic');
      const outer = nodes.filter(n => n.type === 'person' || n.type === 'organization');
      const place = (list, radius, offset) => list.forEach((n, i) => {
        const angle = offset + (i / Math.max(1, list.length)) * Math.PI * 2;
        n.x = cx + Math.cos(angle) * radius;
        n.y = cy + Math.sin(angle) * radius * 0.75;
        n.vx = 0; n.vy = 0;
      });
      const base = Math.min(width, height);
      place(inner, base * 0.2, -Math.PI / 2);
      place(outer, base * 0.38, -Math.PI / 2 + 0.3);
      const you = byId.get('you');
      if(you){ you.x = cx; you.y = cy; you.vx = 0; you.vy = 0; }
    }

    function tick(){
      const cx = width / 2, cy = height / 2;
      // Scale spacing to the canvas so the graph fills it instead of bunching in the middle.
      const base = Math.min(width, height);
      const repulsion = 12 * base;
      const restYou = base * 0.3, restOther = base * 0.18;
      // repulsion + collision
      for(let i = 0; i < nodes.length; i++){
        for(let j = i + 1; j < nodes.length; j++){
          const a = nodes[i], b = nodes[j];
          let dx = b.x - a.x, dy = b.y - a.y;
          let d2 = dx * dx + dy * dy;
          if(d2 < 0.01){ dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.25; }
          const d = Math.sqrt(d2);
          const minD = a.r + b.r + (compact() ? 30 : 18);
          let force = repulsion / d2;
          if(d < minD) force += (minD - d) * 0.5;
          const fx = (dx / d) * force, fy = (dy / d) * force;
          a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
        }
      }
      // springs
      edges.forEach(e => {
        const rest = (e.a.type === 'you' || e.b.type === 'you') ? restYou : restOther;
        const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
        const d = Math.max(0.1, Math.hypot(dx, dy));
        const k = 0.012 + 0.02 * (e.weight / maxWeight);
        const f = (d - rest) * k;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        e.a.vx += fx; e.a.vy += fy; e.b.vx -= fx; e.b.vy -= fy;
      });
      // gravity, damping, integrate, keep inside the canvas
      nodes.forEach(n => {
        if(n.type === 'you' && n !== dragging){ n.x = cx; n.y = cy; n.vx = n.vy = 0; return; }
        if(n === dragging){ n.vx = n.vy = 0; return; }
        n.vx += (cx - n.x) * 0.0025;
        n.vy += (cy - n.y) * 0.004;
        n.vx *= 0.82; n.vy *= 0.82;
        n.x += n.vx * alpha; n.y += n.vy * alpha;
        const pad = n.r + 6;
        n.x = Math.max(pad, Math.min(width - pad, n.x));
        n.y = Math.max(pad, Math.min(height - pad - 28, n.y)); // leave room for the legend
      });
      alpha = Math.max(0, alpha * 0.985);
    }

    function focusSet(){
      const focus = hovered || selected;
      if(!focus) return null;
      const set = new Set(neighbors.get(focus.id));
      set.add(focus.id);
      return set;
    }

    function draw(){
      ctx.clearRect(0, 0, width, height);
      const focus = focusSet();
      const active = hovered || selected;

      edges.forEach(e => {
        const lit = active && (e.a === active || e.b === active);
        const dim = focus && !lit;
        ctx.beginPath();
        ctx.moveTo(e.a.x, e.a.y);
        ctx.lineTo(e.b.x, e.b.y);
        ctx.strokeStyle = lit ? 'rgba(110,231,192,0.65)' : `rgba(136,146,160,${dim ? 0.07 : 0.28})`;
        ctx.lineWidth = 0.8 + 2.6 * (e.weight / maxWeight);
        ctx.stroke();
      });

      nodes.forEach(n => {
        const meta = TYPE_META[n.type];
        const dim = focus && !focus.has(n.id);
        ctx.globalAlpha = dim ? 0.25 : 1;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
        ctx.fillStyle = meta.color;
        ctx.shadowColor = meta.color;
        ctx.shadowBlur = n.type === 'you' ? 28 : (n === active ? 22 : 10);
        ctx.fill();
        ctx.shadowBlur = 0;
        if(n === selected){
          ctx.beginPath();
          ctx.arc(n.x, n.y, n.r + 5, 0, Math.PI * 2);
          ctx.strokeStyle = meta.color;
          ctx.lineWidth = 2;
          ctx.stroke();
        }

        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        if(n.type === 'you'){
          ctx.font = '600 13px Inter, sans-serif';
          ctx.fillStyle = '#0B0E14';
          ctx.fillText('You', n.x, n.y);
        }else if(n === active || (focus && focus.has(n.id)) || n.type === 'topic'
                 || (showAllLabels && !(compact() && n.type === 'organization'))){
          const label = n.label.length > 24 ? n.label.slice(0, 23) + '…' : n.label;
          ctx.font = `${n.type === 'topic' ? 600 : 500} 11.5px Inter, sans-serif`;
          // Keep the label inside the canvas even when its node sits near an edge.
          const half = ctx.measureText(label).width / 2;
          const lx = Math.max(half + 4, Math.min(width - half - 4, n.x));
          ctx.lineWidth = 3;
          ctx.strokeStyle = 'rgba(19,23,32,0.9)';
          ctx.strokeText(label, lx, n.y + n.r + 12);
          ctx.fillStyle = '#E8ECF1';
          ctx.fillText(label, lx, n.y + n.r + 12);
        }
        ctx.globalAlpha = 1;
      });
    }

    function loop(){
      if(alpha > 0.004 || dragging){ tick(); }
      draw();
      raf = (alpha > 0.004 || dragging) ? requestAnimationFrame(loop) : null;
    }
    function reheat(amount = 0.8){
      alpha = Math.max(alpha, amount);
      if(!raf) raf = requestAnimationFrame(loop);
    }
    function redraw(){ if(!raf) draw(); }

    function nodeAt(mx, my){
      for(let i = nodes.length - 1; i >= 0; i--){
        const n = nodes[i];
        if(Math.hypot(n.x - mx, n.y - my) <= n.r + 4) return n;
      }
      return null;
    }
    const pointer = (e) => {
      const rect = canvas.getBoundingClientRect();
      return [e.clientX - rect.left, e.clientY - rect.top];
    };

    let downAt = null;
    canvas.addEventListener('pointerdown', (e) => {
      const [mx, my] = pointer(e);
      const hit = nodeAt(mx, my);
      downAt = [mx, my];
      if(hit && hit.type !== 'you'){
        dragging = hit;
        canvas.setPointerCapture(e.pointerId);
        reheat(0.3);
      }
    });
    canvas.addEventListener('pointermove', (e) => {
      const [mx, my] = pointer(e);
      if(dragging){
        dragging.x = mx; dragging.y = my;
        return;
      }
      const hit = nodeAt(mx, my);
      if(hit !== hovered){
        hovered = hit;
        canvas.style.cursor = hit ? 'pointer' : 'default';
        redraw();
      }
    });
    canvas.addEventListener('pointerleave', () => { if(!dragging){ hovered = null; redraw(); } });
    canvas.addEventListener('pointerup', (e) => {
      const [mx, my] = pointer(e);
      const moved = downAt && Math.hypot(mx - downAt[0], my - downAt[1]) > 4;
      if(dragging){ canvas.releasePointerCapture(e.pointerId); dragging = null; reheat(0.2); }
      if(!moved){
        const hit = nodeAt(mx, my);
        select(hit ? hit.id : 'you');
      }
      downAt = null;
    });

    function select(id){
      selected = byId.get(id) || byId.get('you');
      onSelect(selected);
      redraw();
    }

    new ResizeObserver(resize).observe(canvas.parentElement);
    resize();
    return { select, neighbors: (id) => neighbors.get(id), node: (id) => byId.get(id) };
  }

  /* ---------------- page ---------------- */
  async function init(){
    const canvas = document.querySelector('[data-graph-canvas]');
    if(!canvas) return;
    const overlay = document.querySelector('[data-graph-overlay]');
    const legend = document.querySelector('[data-graph-legend]');
    const detail = document.querySelector('[data-node-detail]');
    const about = document.querySelector('[data-graph-about]');

    const showOverlay = (html) => { overlay.innerHTML = html; overlay.hidden = false; };
    showOverlay('<div class="graph-overlay-msg"><div class="spinner"></div><p>Mapping your memories…</p></div>');

    let graph;
    try{
      const res = await fetch(`${API_BASE}/graph/`, { credentials: 'include' });
      if(res.status === 401) return; // initAuthGuard redirects
      if(!res.ok) throw new Error(String(res.status));
      graph = await res.json();
    }catch(err){
      showOverlay('<div class="graph-overlay-msg"><p>Couldn\'t load your graph. Try refreshing.</p></div>');
      return;
    }

    const counts = { person: 0, organization: 0, topic: 0 };
    graph.nodes.forEach(n => { if(n.type in counts) counts[n.type]++; });

    legend.innerHTML = [['you', 'You'], ...Object.keys(counts).filter(t => counts[t]).map(t => [t, `${TYPE_META[t].label} · ${counts[t]}`])]
      .map(([t, text]) => `<span><span class="dot" style="background:${TYPE_META[t].color}"></span>${esc(text)}</span>`)
      .join('');

    about.innerHTML = `
      <p>People and organizations come from your email senders and calendar attendees; automated senders (newsletters, no-reply addresses) are grouped by company. Topics are clusters of related memories, named from their titles.</p>
      <p>A bigger node means more memories. A thicker line means more memories in common. Drag nodes to rearrange.</p>
      <div class="graph-about-stats">
        Built from ${plural(graph.total_memories, 'memory', 'memories')}${graph.clustered_memories ? `, ${plural(graph.clustered_memories, 'memory', 'memories')} grouped into topics` : ''}. Updated ${esc(timeAgo(graph.generated_at))}.
      </div>`;

    // Nothing to connect yet: explain instead of drawing a lone "You" under the message.
    if(graph.nodes.length <= 1){
      legend.innerHTML = '';
      showOverlay(graph.total_memories
        ? '<div class="graph-overlay-msg"><p>Not enough connections yet. As more emails, events and uploads come in, your twin will map the people and topics in them.</p></div>'
        : '<div class="graph-overlay-msg"><p>Your graph is empty. <a href="connectors.html">Connect a source</a> and your twin will map the people and topics in your memories.</p></div>');
      detail.innerHTML = `
        <div class="card-head"><h3>Node detail</h3></div>
        <p class="node-intro">${graph.total_memories
          ? `Your twin has ${plural(graph.total_memories, 'memory', 'memories')}, but no people or topics to connect yet.`
          : 'Once your twin has memories, click any node here to see what it knows about that person or topic.'}</p>`;
      return;
    }
    overlay.hidden = true;

    const renderer = createRenderer(canvas, graph, (node) => renderDetail(node));

    function renderDetail(node){
      const meta = TYPE_META[node.type];
      const nb = [...(renderer.neighbors(node.id) || [])]
        .map(id => {
          const other = renderer.node(id);
          const edge = graph.edges.find(e => (e.source === node.id && e.target === id) || (e.target === node.id && e.source === id));
          return { node: other, weight: edge ? edge.weight : 0 };
        })
        .filter(x => x.node && x.node.type !== 'you')
        .sort((a, b) => b.weight - a.weight);

      const span = node.first_seen
        ? (shortDate(node.first_seen) === shortDate(node.last_seen) ? shortDate(node.last_seen) : `${shortDate(node.first_seen)} – ${shortDate(node.last_seen)}`)
        : '';
      const sources = Object.entries(node.sources || {})
        .sort((a, b) => b[1] - a[1])
        .map(([s, n]) => `${esc(sourceLabel(s))} ${n}`)
        .join(' · ');

      const intro = node.type === 'you'
        ? `<p class="node-intro">${plural(node.count, 'memory', 'memories')} across ${plural(Object.keys(node.sources || {}).length, 'source', 'sources')}. Click any node to see how it connects to you.</p>`
        : '';
      detail.innerHTML = `
        <div class="card-head"><h3>Node detail</h3></div>
        <div class="node-type"><span class="dot" style="background:${meta.color}"></span>${esc(node.type === 'you' ? 'You' : meta.single)}</div>
        <h4 class="node-title">${esc(node.type === 'you' ? 'You' : node.label)}</h4>
        ${node.subtitle && node.type !== 'you' ? `<div class="node-subtitle">${esc(node.subtitle)}</div>` : ''}
        ${intro}
        ${node.type !== 'you' ? `<div class="node-stats">${plural(node.count, 'memory', 'memories')}${span ? ` · ${esc(span)}` : ''}</div>` : ''}
        ${sources ? `<div class="node-sources">${sources}</div>` : ''}
        ${nb.length ? `
          <div class="node-section-label">Connected to</div>
          <div class="node-links">${nb.slice(0, 12).map(x => `
            <button type="button" class="node-link" data-select="${esc(x.node.id)}">
              <span class="dot" style="background:${TYPE_META[x.node.type].color}"></span>${esc(x.node.label)}<span class="node-link-n">${x.weight}</span>
            </button>`).join('')}
          </div>` : ''}
        ${node.recent && node.recent.length ? `
          <div class="node-section-label">Recent memories</div>
          <ul class="node-recent">${node.recent.map(r => `
            <li><div class="t">${esc(r.title)}</div><div class="m">${esc(sourceLabel(r.source))} · ${esc(shortDate(r.occurred_at))}</div></li>`).join('')}
          </ul>` : ''}`;
    }

    detail.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-select]');
      if(btn) renderer.select(btn.dataset.select);
    });
    renderer.select('you');
  }

  return { init };
})();
