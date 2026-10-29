import micromark from 'micromark';

/** One CommonMark renderer for the OpenBoxes chat and the local workflow console. */
function renderChatMarkdown(value) {
  return micromark(String(value || ''), {
    allowDangerousHtml: false,
    allowDangerousProtocol: false,
  });
}

export { renderChatMarkdown };
export default renderChatMarkdown;
