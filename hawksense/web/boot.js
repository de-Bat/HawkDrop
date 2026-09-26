// Runs before the app: carry an access token into the manifest URL so an app
// added to the iOS home screen starts with it. (External file so the strict
// Content-Security-Policy can forbid inline scripts.)
(function () {
  var token = new URLSearchParams(location.search).get('token');
  if (token) document.getElementById('manifest-link').href = 'manifest.webmanifest?token=' + encodeURIComponent(token);
})();
