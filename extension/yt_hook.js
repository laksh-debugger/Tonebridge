// Runs INSIDE the YouTube page (not in the extension's private world),
// because only there can we ask the YouTube player which caption tracks exist.
// It answers questions sent by content.js through window.postMessage.
(function () {
  if (window.__tbHook) return;
  window.__tbHook = true;
  window.addEventListener("message", function (e) {
    if (e.source !== window || !e.data || e.data.tb !== "TB_TRACKS") return;
    var payload = { tracks: [], videoId: null };
    try {
      var player = document.getElementById("movie_player");
      var response = player && player.getPlayerResponse ? player.getPlayerResponse() : window.ytInitialPlayerResponse;
      var list = response && response.captions && response.captions.playerCaptionsTracklistRenderer
        && response.captions.playerCaptionsTracklistRenderer.captionTracks;
      payload.videoId = response && response.videoDetails && response.videoDetails.videoId;
      (list || []).forEach(function (t) {
        payload.tracks.push({ baseUrl: t.baseUrl, languageCode: t.languageCode, kind: t.kind || "" });
      });
    } catch (err) { /* leave empty */ }
    window.postMessage({ tb: "TB_TRACKS:reply", id: e.data.id, payload: payload }, "*");
  });
})();
