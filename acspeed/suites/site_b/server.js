'use strict';

// Alcove, a small single-page site.
//
// Every path serves the same page, so the site root and any deep link render the page. sendFile reads
// the file from disk per request, so an edit to public/index.html takes effect on the next request.

const express = require('express');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;
const INDEX = path.join(__dirname, 'public', 'index.html');

// A conventional health endpoint, defined before the catch-all so it is not shadowed by it.
app.get('/healthz', function (_req, res) {
  res.type('text').send('ok');
});

app.use(function (_req, res) {
  res.sendFile(INDEX);
});

app.listen(PORT, function () {
  console.log('alcove listening on http://localhost:' + PORT);
});
