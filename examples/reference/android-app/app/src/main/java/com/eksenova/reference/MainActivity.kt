package com.eksenova.reference

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

class MainActivity : Activity() {
    private val invoices = mutableListOf("Harman Kuyumculuk", "Beyaz Altın Toptan")
    private lateinit var list: LinearLayout
    private lateinit var status: TextView
    private lateinit var permission: TextView
    private lateinit var route: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 96, 48, 48)
            setBackgroundColor(Color.rgb(246, 245, 241))
        }
        root.addView(TextView(this).apply {
            id = R.id.title
            text = "Faturalar"
            textSize = 28f
        })
        route = TextView(this).apply { id = R.id.route; text = "ana sayfa" }
        root.addView(route)
        list = LinearLayout(this).apply { id = R.id.invoice_list; orientation = LinearLayout.VERTICAL }
        root.addView(list)
        val customer = EditText(this).apply { id = R.id.customer; hint = "Müşteri" }
        root.addView(customer, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        status = TextView(this).apply { id = R.id.status }
        root.addView(Button(this).apply {
            id = R.id.save
            text = "Kaydet"
            setOnClickListener {
                val name = customer.text.toString().trim()
                if (name.isEmpty()) {
                    status.text = "Müşteri zorunlu"
                } else {
                    invoices.add(name)
                    customer.setText("")
                    status.text = "Kaydedildi: $name"
                    render()
                }
            }
        })
        root.addView(status)
        root.addView(Button(this).apply {
            id = R.id.delete
            text = "Sil"
            setOnClickListener {
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("Silme onayı")
                    .setMessage("Son fatura silinsin mi?")
                    .setPositiveButton("Evet, sil") { _, _ ->
                        if (invoices.isNotEmpty()) invoices.removeAt(invoices.size - 1)
                        status.text = "Silindi"
                        render()
                    }
                    .setNegativeButton("Vazgeç", null)
                    .show()
            }
        })
        root.addView(Button(this).apply {
            id = R.id.camera
            text = "Kamera izni iste"
            setOnClickListener { requestPermissions(arrayOf(Manifest.permission.CAMERA), CAMERA_REQUEST) }
        })
        permission = TextView(this).apply { id = R.id.permission }
        root.addView(permission)
        setContentView(root)
        render()
        showPermission()
        handle(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    private fun handle(intent: Intent?) {
        val data = intent?.data ?: return
        route.text = "${data.host}${data.path ?: ""}"
    }

    private fun render() {
        list.removeAllViews()
        invoices.forEachIndexed { index, name ->
            list.addView(TextView(this).apply {
                text = name
                textSize = 18f
                contentDescription = "fatura ${index + 1}"
                setPadding(0, 16, 0, 16)
            })
        }
    }

    private fun showPermission() {
        val granted = checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        permission.text = if (granted) "Kamera izni: verildi" else "Kamera izni: yok"
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == CAMERA_REQUEST) {
            val granted = grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
            permission.text = if (granted) "Kamera izni: verildi" else "Kamera izni: reddedildi"
        }
    }

    companion object {
        private const val CAMERA_REQUEST = 7
    }
}
