"""Progressive presentation of a completed answer; no model or network calls."""

SCRIPT = r"""
window.CampusPulseAnswerReveal = class {
  finish() {
    const state = this.state;
    if (!state) return;
    this.state = null;
    cancelAnimationFrame(state.frame);
    document.removeEventListener('visibilitychange', state.onHidden);
    const follow = state.root.isConnected && this.atBottom(state.scrollport);
    state.text.forEach(({node, value}) => { node.data = value; });
    state.blocks.forEach(block => { block.hidden = false; block.classList.remove('chat-reveal-part'); });
    state.root.removeAttribute('aria-busy');
    state.log.removeAttribute('aria-busy');
    if (follow) state.scrollport.scrollTo({top: state.scrollport.scrollHeight, behavior: 'instant'});
  }
  atBottom(scrollport) {
    return scrollport.scrollHeight - scrollport.scrollTop - scrollport.clientHeight < 48;
  }
  start(root, scrollport, log) {
    this.finish();
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    if (reduced.matches || document.hidden) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const text = [], words = [];
    while (walker.nextNode()) {
      const node = walker.currentNode, value = node.data;
      text.push({node, value});
      for (const word of value.match(/\S+\s*|\s+/gu) || []) words.push({node, word});
    }
    if (words.length < 2) return;
    const state = {root, scrollport, log, text, words, shown: 0, frame: null, began: null,
      duration: Math.min(6500, Math.max(280, words.length * 28)),
      blocks: Array.from(root.querySelectorAll(':scope > *, li'))};
    state.onHidden = () => { if (document.hidden) this.finish(); };
    document.addEventListener('visibilitychange', state.onHidden);
    this.state = state;
    text.forEach(({node}) => { node.data = ''; });
    state.blocks.forEach(block => { block.hidden = true; });
    root.setAttribute('aria-busy', 'true');
    log.setAttribute('aria-busy', 'true');
    const tick = now => {
      if (this.state !== state) return;
      if (!root.isConnected || document.hidden || reduced.matches) { this.finish(); return; }
      state.began ??= now;
      const follow = this.atBottom(scrollport);
      const count = Math.min(words.length, Math.max(1, Math.ceil((now - state.began) / state.duration * words.length)));
      while (state.shown < count) {
        const {node, word} = words[state.shown++];
        node.data += word;
        for (let block = node.parentElement; block && block !== root; block = block.parentElement) {
          if (block.hidden) { block.hidden = false; block.classList.add('chat-reveal-part'); }
        }
      }
      if (follow) scrollport.scrollTo({top: scrollport.scrollHeight, behavior: 'instant'});
      if (state.shown === words.length) this.finish();
      else state.frame = requestAnimationFrame(tick);
    };
    state.frame = requestAnimationFrame(tick);
  }
};
"""
