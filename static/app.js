// Menampilkan kolom alergi hanya jika pengguna memilih "Punya". (Tanpa inline script agar sesuai CSP.)
document.addEventListener("DOMContentLoaded", function () {
  var box = document.getElementById("allergy-box");
  if (!box) return;
  var radios = document.querySelectorAll('input[name="has_allergy"]');
  function sync() {
    var checked = document.querySelector('input[name="has_allergy"]:checked');
    box.hidden = !(checked && checked.value === "1");
  }
  radios.forEach(function (r) { r.addEventListener("change", sync); });
  sync();
});
