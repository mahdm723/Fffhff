// V6 phase 8: the app's name, from APP_NAME (the server writes it into <html data-app>). Never hard-code it.
export function appName() {
  return document.documentElement.dataset.app || 'DALTA.BIT';
}

// The wordmark: a dot in the name becomes the gradient dot (DALTA.BIT → DALTA●BIT); otherwise the dot follows it.
export function wordmarkParts() {
  const name = appName();
  const i = name.indexOf('.');
  return i > 0 ? [name.slice(0, i), name.slice(i + 1)] : [name, ''];
}
