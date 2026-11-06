// Deal math: typing a multiple fills the purchase price (multiple x cash flow).
(function () {
  var m = document.getElementById("multiple");
  var cf = document.querySelector("[name=cash_flow]");
  var price = document.querySelector("[name=purchase_price]");
  if (!m || !cf || !price) return;
  function fill() { var x = parseFloat(m.value), c = parseFloat(cf.value); if (x > 0 && c > 0) price.value = Math.round(x * c); }
  m.addEventListener("input", fill); cf.addEventListener("input", fill);
})();
