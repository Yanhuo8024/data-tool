(function (root) {
  'use strict';

  /**
   * Split model-emitted <think> blocks from the user-facing answer.
   * Handles multiple blocks, HTML-escaped tags and an unfinished block while streaming.
   */
  function splitThinkContent(value) {
    const raw = String(value == null ? '' : value);
    const tokenRe = /<\s*(\/?)\s*think\s*>|&lt;\s*(\/?)\s*think\s*&gt;/gi;
    const thinking = [];
    const answer = [];
    let cursor = 0;
    let inside = false;
    let match;

    while ((match = tokenRe.exec(raw)) !== null) {
      const chunk = raw.slice(cursor, match.index);
      (inside ? thinking : answer).push(chunk);
      const isClosing = Boolean(match[1] || match[2]);
      inside = !isClosing;
      cursor = tokenRe.lastIndex;
    }

    const tail = raw.slice(cursor);
    (inside ? thinking : answer).push(tail);
    return {
      thinking: thinking.join('').trim(),
      answer: answer.join('').trimStart(),
      inThink: inside,
    };
  }

  if (root) root.splitThinkContent = splitThinkContent;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { splitThinkContent: splitThinkContent };
  }
})(typeof window !== 'undefined' ? window : null);
