# -*- coding: utf-8 -*-
# Copyright (c) 2021, Aakvatech and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from frappe.model.document import Document
from hms_tz.nhif.api.healthcare_utils import (
    get_item_rate,
    get_warehouse_from_service_unit,
    get_template_company_option,
    msgThrow,
    validate_nhif_patient_claim_status,
)
from hms_tz.nhif.api.patient_encounter import validate_stock_item
from hms_tz.nhif.api.patient_appointment import get_mop_amount, get_discount_percent
from frappe.model.workflow import apply_workflow
from frappe.utils import get_url_to_form, nowdate
from hms_tz.nhif.api.patient_encounter import get_drug_quantity


class MedicationChangeRequest(Document):
    def before_insert(self):
        if not self.sales_order and not self.delivery_note:
            frappe.throw(
                "Please select either Sales Order or Delivery Note to create Medication Change Request"
            )

        if not self.sales_order and self.delivery_note:
            validate_nhif_patient_claim_status(
                "Medication Change Request", self.company, self.appointment
            )

        if self.patient_encounter:
            encounter_doc = get_patient_encounter_doc(self.patient_encounter)
            if (
                not encounter_doc.insurance_coverage_plan
                and not encounter_doc.inpatient_record
                and not self.sales_order
            ):
                frappe.throw(
                    frappe.bold(
                        "Cannot create medication change request for Cash Patient OPD via delivery note,<br>\
                            Please create medication change request for Cash Patient OPD via Sales Order"
                    )
                )

            self.warehouse = self.get_warehouse_per_dn_or_so()

            for row in encounter_doc.drug_prescription:
                if (
                    self.delivery_note
                    and self.warehouse
                    != get_warehouse_from_service_unit(row.healthcare_service_unit)
                ):
                    continue

                self.set_drugs(
                    row,
                    insurance_subscription=encounter_doc.insurance_subscription,
                    inpatient_record=encounter_doc.inpatient_record,
                )

            if not self.patient_encounter_final_diagnosis:
                for d in encounter_doc.patient_encounter_final_diagnosis:
                    if not isinstance(d, dict):
                        d = d.as_dict()

                    d["name"] = None

                    self.append("patient_encounter_final_diagnosis", d)

    def after_insert(self):
        if self.delivery_note:
            self.update_delivery_note_workflow("Changes Requested", "Request Changes")

        if self.sales_order:
            url = get_url_to_form("Medication Change Request", self.name)
            comment = frappe.get_doc(
                {
                    "doctype": "Comment",
                    "comment_type": "Comment",
                    "comment_email": frappe.session.user,
                    "reference_doctype": "Sales Order",
                    "reference_name": self.sales_order,
                    "content": f"Medication Change Request: <a href='{url}'>{frappe.bold(self.name)}</a> is created",
                }
            ).insert(ignore_permissions=True)

            frappe.db.set_value(
                "Sales Order", self.sales_order, "med_change_request_status", "Pending"
            )

    def validate(self):
        self.validate_duplicate_medication_change_request()

        self.title = (
            f"{self.patient_encounter}/{self.delivery_note or self.sales_order}"
        )
        self.warehouse = self.get_warehouse_per_dn_or_so()

        items = []
        if self.drug_prescription:
            for drug in self.drug_prescription:
                if drug.drug_code not in items:
                    items.append(drug.drug_code)
                else:
                    frappe.throw(
                        _(
                            f"Drug '{frappe.bold(drug.drug_code)}' is duplicated in line '{frappe.bold(drug.idx)}' in Drug Prescription"
                        )
                    )

                self.validate_drug_quantity(drug)
                self.validate_item_available_in_house(drug)

                if not self.sales_order:
                    self.validate_item_insurance_coverage(drug, "validate")
                    validate_healthcare_service_unit(
                        self.warehouse, drug, method="validate"
                    )

    def before_submit(self):
        if not self.sales_order:
            validate_nhif_patient_claim_status(
                "Medication Change Request", self.company, self.appointment
            )

        self.warehouse = self.get_warehouse_per_dn_or_so()
        for item in self.drug_prescription:
            if not self.sales_order:
                self.validate_item_insurance_coverage(item, "throw")
                validate_healthcare_service_unit(self.warehouse, item, method="throw")

            set_amount(self, item)

    def on_submit(self):
        if not self.sales_order:
            validate_nhif_patient_claim_status(
                "Medication Change Request", self.company, self.appointment
            )

        encounter_doc = self.update_encounter()

        if self.delivery_note:
            self.update_delivery_note(encounter_doc)

        if self.sales_order:
            self.update_sales_order(encounter_doc)

    def on_trash(self):
        if self.sales_order:
            url = get_url_to_form("Medication Change Request", self.name)
            comment = frappe.get_doc(
                {
                    "doctype": "Comment",
                    "comment_type": "Comment",
                    "comment_email": frappe.session.user,
                    "reference_doctype": "Sales Order",
                    "reference_name": self.sales_order,
                    "content": f"Medication Change Request: <a href='{url}'>{frappe.bold(self.name)}</a> is deleted",
                }
            ).insert(ignore_permissions=True)

            frappe.db.set_value(
                "Sales Order", self.sales_order, "med_change_request_status", ""
            )

    def set_drugs(self, row, insurance_subscription=None, inpatient_record=None):
        is_so_from_encounter = frappe.get_cached_value(
            "Company", self.company, "auto_create_sales_order_from_encounter"
        )
        if insurance_subscription:
            # add only covered items unser insurance coverage, means items that reached to delivery note
            if self.delivery_note and row.prescribe == 0:
                new_row = row.as_dict()
                new_row["name"] = None
                new_row["parent"] = None
                new_row["parentfield"] = None
                new_row["parenttype"] = None

                self.append("original_pharmacy_prescription", new_row)
                self.append("drug_prescription", new_row)

            # add only uncovered items that are prescribed, means items that reached to sales order
            if is_so_from_encounter == 1 and self.sales_order and row.prescribe == 1:
                new_row = row.as_dict()
                new_row["name"] = None
                new_row["parent"] = None
                new_row["parentfield"] = None
                new_row["parenttype"] = None

                self.append("original_pharmacy_prescription", new_row)
                self.append("drug_prescription", new_row)

        elif not insurance_subscription:
            # add all cash items from sales order for OPD patient
            if is_so_from_encounter == 1 and self.sales_order and row.prescribe == 1:
                new_row = row.as_dict()
                new_row["name"] = None
                new_row["parent"] = None
                new_row["parentfield"] = None
                new_row["parenttype"] = None

                self.append("original_pharmacy_prescription", new_row)
                self.append("drug_prescription", new_row)

            # add all cash items from pe for ipd patient
            if inpatient_record and row.prescribe == 1:
                frappe.msgprint("not insurance_subscription inpatient_record prescribe=1")
                new_row = row.as_dict()
                new_row["name"] = None
                new_row["parent"] = None
                new_row["parentfield"] = None
                new_row["parenttype"] = None

                self.append("original_pharmacy_prescription", new_row)
                self.append("drug_prescription", new_row)

    def validate_drug_quantity(self, row):
        # auto calculating quantity
        if not row.quantity:
            row.quantity = get_drug_quantity(row)

        if not row.quantity:
            frappe.throw(
                "Please keep quantity for item: {frappe.bold(row.drug_code)}, Row#: {frappe.bold(row.idx)}"
            )

        row.delivered_quantity = row.quantity - (row.quantity_returned or 0)

        validate_stock_item(
            row.drug_code,
            row.quantity,
            self.company,
            row.doctype,
            row.healthcare_service_unit,
            caller="unknown",
            method="validate",
        )

    def validate_item_available_in_house(self, row):
        template_doc = get_template_company_option(row.drug_code, self.company)
        row.is_not_available_inhouse = template_doc.is_not_available
        if row.is_not_available_inhouse == 1:
            frappe.msgprint(
                "NOTE: This healthcare service item, <b> {frappe.bold(row.drug_code)} </b>, is not available inhouse"
            )

    def validate_duplicate_medication_change_request(self):
        if not self.patient_encounter:
            return

        if self.delivery_note:
            meds = frappe.db.get_all(
                "Medication Change Request",
                filters={
                    "patient_encounter": self.patient_encounter,
                    "delivery_note": self.delivery_note,
                    "docstatus": 0,
                    "name": ["!=", self.name],
                },
            )
            if len(meds) > 0:
                url = get_url_to_form("Medication Change Request", meds[0].name)
                msg = f"""<div style="border: 1px solid #ccc; background-color: #f9f9f9; padding: 10px; border-radius: 5px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); margin: 10px;">
                    <p style="font-weight: normal; font-size: 15px;">Draft Medication Change Request: <span style="font-weight: bold;"><a href='{url}'>{frappe.bold(meds[0].name)}</a></span>\
                        for delivery note: <span style="font-weight: bold;">{frappe.bold(self.delivery_note)}</span>\
                            and patient encounter: <span style="font-weight: bold;">{frappe.bold(self.patient_encounter)}</span> is already created</p>
                    <p style="font-style: italic; font-weight: bold; font-size: 15px;"></p>
                    <p style="font-size: 15px;">Please update the existing Medication Change Request or delete it and try again.</p>
                </div>"""

                frappe.throw(msg)

        if self.sales_order:
            meds = frappe.db.get_all(
                "Medication Change Request",
                filters={
                    "patient_encounter": self.patient_encounter,
                    "sales_order": self.sales_order,
                    "docstatus": 0,
                    "name": ["!=", self.name],
                },
            )
            if len(meds) > 0:
                url = get_url_to_form("Medication Change Request", meds[0].name)
                msg = f"""<div style="border: 1px solid #ccc; background-color: #f9f9f9; padding: 10px; border-radius: 5px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); margin: 10px;">
                    <p style="font-weight: normal; font-size: 15px;">Draft Medication Change Request: <span style="font-weight: bold;"><a href='{url}'>{frappe.bold(meds[0].name)}</a></span>\
                        for sales order: <span style="font-weight: bold;">{frappe.bold(self.sales_order)}</span>\
                            and patient encounter: <span style="font-weight: bold;">{frappe.bold(self.patient_encounter)}</span> is already created</p>
                    <p style="font-style: italic; font-weight: bold; font-size: 15px;"></p>
                    <p style="font-size: 15px;">Please update the existing Medication Change Request or delete it and try again.</p>
                </div>"""

                frappe.throw(msg)

    def get_warehouse_per_dn_or_so(self):
        if self.sales_order:
            return frappe.get_value("Sales Order", self.sales_order, "set_warehouse")

        if self.delivery_note:
            return frappe.get_value(
                "Delivery Note", self.delivery_note, "set_warehouse"
            )

    def validate_item_insurance_coverage(self, row, method):
        """Validate if the Item is covered with the insurance coverage plan of a patient"""
        if row.prescribe:
            return

        insurance_subscription, insurance_company, mop = get_insurance_details(self)
        if mop:
            return

        insurance_coverage_plan = frappe.get_cached_value(
            "Healthcare Insurance Subscription",
            {"name": insurance_subscription},
            "healthcare_insurance_coverage_plan",
        )
        if not insurance_coverage_plan:
            frappe.throw(_("Healthcare Insurance Coverage Plan is Not defiend"))

        coverage_plan_name, is_exclusions = frappe.get_cached_value(
            "Healthcare Insurance Coverage Plan",
            insurance_coverage_plan,
            ["coverage_plan_name", "is_exclusions"],
        )

        today = frappe.utils.nowdate()
        service_coverage = frappe.get_all(
            "Healthcare Service Insurance Coverage",
            filters={
                "is_active": 1,
                "start_date": ["<=", today],
                "end_date": [">=", today],
                "healthcare_service_template": row.drug_code,
                "healthcare_insurance_coverage_plan": insurance_coverage_plan,
            },
            fields=[
                "name",
                "approval_mandatory_for_claim",
                "healthcare_service_template",
            ],
        )
        if service_coverage:
            row.is_restricted = service_coverage[0].approval_mandatory_for_claim

            if is_exclusions:
                msgThrow(
                    _(
                        "{0} not covered in Healthcare Insurance Coverage Plan "
                        + str(frappe.bold(coverage_plan_name))
                    ).format(frappe.bold(row.drug_code)),
                    method,
                )

        else:
            if not is_exclusions:
                msgThrow(
                    _(
                        "{0} not covered in Healthcare Insurance Coverage Plan "
                        + str(frappe.bold(coverage_plan_name))
                    ).format(frappe.bold(row.drug_code)),
                    method,
                )

    def update_encounter(self):
        doc = frappe.get_doc("Patient Encounter", self.patient_encounter)
        for line in self.original_pharmacy_prescription:
            for row in doc.drug_prescription:
                if (
                    line.drug_code == row.drug_code
                    and line.healthcare_service_unit == row.healthcare_service_unit
                ):
                    frappe.delete_doc(
                        row.doctype,
                        row.name,
                        force=1,
                        ignore_permissions=True,
                        for_reload=True,
                    )
        doc.reload()
        fields_to_clear = [
            "name",
            "owner",
            "creation",
            "modified",
            "modified_by",
            "docstatus",
            "amended_from",
            "amendment_date",
            "parentfield",
            "parenttype",
        ]
        for row in self.drug_prescription:
            if row.is_not_available_inhouse == 1:
                continue
            new_row = frappe.copy_doc(row).as_dict()
            for fieldname in fields_to_clear:
                new_row[fieldname] = None
            new_row["drug_prescription_created"] = 1
            doc.append("drug_prescription", new_row)
        doc.db_update_all()
        frappe.msgprint(
            _("Patient Encounter " + self.patient_encounter + " has been updated!"),
            alert=True,
        )
        return doc

    def update_sales_order(self, encounter_doc):
        so_doc = frappe.get_doc("Sales Order", self.sales_order)
        so_doc.items = []
        for row in encounter_doc.get("drug_prescription"):
            if (
                row.prescribe == 0
                or row.is_not_available_inhouse == 1
                or row.is_cancelled == 1
            ):
                continue

            item_code = frappe.get_value("Medication", row.get("drug_code"), "item")
            if not item_code:
                frappe.throw(
                    _(
                        f"""The Item Code for {row.get("drug_code")} is not found!<br>\
                            Please request administrator to set item code in {row.get("drug_code")}."""
                    )
                )

            item_name, item_description = frappe.get_value(
                "Item", item_code, ["item_name", "description"]
            )

            dosage_info = ", <br>".join(
                [
                    "frequency: " + str(row.get("dosage") or "No Prescription Dosage"),
                    "period: " + str(row.get("period") or "No Prescription Period"),
                    "dosage_form: " + str(row.get("dosage_form") or ""),
                    "interval: " + str(row.get("interval") or ""),
                    "interval_uom: " + str(row.get("interval_uom") or ""),
                    "medical_code: "
                    + str(row.get("medical_code") or "No medical code"),
                    "Doctor's comment: "
                    + (row.get("comment") or "Take medication as per dosage."),
                ]
            )

            new_row = {
                "item_code": item_code,
                "item_name": item_name,
                "description": item_description,
                "dosage_info": dosage_info,
                "qty": row.quantity - row.quantity_returned,
                "delivery_date": nowdate(),
                "warehouse": so_doc.set_warehouse,
                "reference_dt": row.get("doctype"),
                "reference_dn": row.get("name"),
                "healthcare_practitioner": encounter_doc.get("practitioner"),
                "healthcare_service_unit": encounter_doc.get("healthcare_service_unit"),
            }

            so_doc.append("items", new_row)

        so_doc.med_change_request_status = "Approved"
        so_doc.save(ignore_permissions=True)
        so_doc.add_comment(
            comment_type="Comment",
            text=f"Medication Change Request: {frappe.bold(self.name)} is Approved",
        )
        so_doc.reload()

    def update_delivery_note(self, encounter_doc):
        dn_doc = frappe.get_doc("Delivery Note", self.delivery_note)
        dn_doc.items = []
        dn_doc.hms_tz_original_items = []

        for row in encounter_doc.drug_prescription:
            warehouse = get_warehouse_from_service_unit(row.healthcare_service_unit)
            if warehouse != dn_doc.set_warehouse:
                continue

            if (
                row.prescribe and 
                (
                    encounter_doc.insurance_subscription or
                    (
                        not encounter_doc.inpatient_record and
                        not encounter_doc.insurance_subscription
                    )
                )
            ):
                continue

            if row.is_not_available_inhouse or row.is_cancelled:
                continue

            item_code, uom = frappe.get_cached_value(
                "Medication", row.drug_code, ["item", "stock_uom"]
            )
            is_stock, item_name = frappe.get_cached_value(
                "Item", item_code, ["is_stock_item", "item_name"]
            )
            if not is_stock:
                continue
            item = frappe.new_doc("Delivery Note Item")
            item.item_code = item_code
            item.item_name = item_name
            item.warehouse = warehouse
            item.qty = row.delivered_quantity or 1
            item.medical_code = row.medical_code
            item.rate = row.amount
            item.amount = row.amount * row.delivered_quantity
            item.reference_doctype = row.doctype
            item.reference_name = row.name
            item.is_restricted = row.is_restricted
            item.discount_percentage = row.hms_tz_is_discount_percent
            item.hms_tz_is_discount_applied = row.hms_tz_is_discount_applied
            item.description = ", <br>".join(
                [
                    "frequency: " + str(row.get("dosage") or "No Prescription Dosage"),
                    "period: " + str(row.get("period") or "No Prescription Period"),
                    "dosage_form: " + str(row.get("dosage_form") or ""),
                    "interval: " + str(row.get("interval") or ""),
                    "interval_uom: " + str(row.get("interval_uom") or ""),
                    "medical_code: "
                    + str(row.get("medical_code") or "No medical code"),
                    "Doctor's comment: "
                    + (row.get("comment") or "Take medication as per dosage."),
                ]
            )
            dn_doc.append("items", item)

            new_original_item = set_original_items(dn_doc.name, item)
            new_original_item.stock_uom = uom
            new_original_item.uom = uom
            dn_doc.append("hms_tz_original_items", new_original_item)

        dn_doc.save(ignore_permissions=True)
        dn_doc.reload()

        self.update_delivery_note_workflow(
            "Changes Made", "Make Changes", dn_doc=dn_doc
        )

        self.update_drug_prescription(encounter_doc, dn_doc)


    def update_delivery_note_workflow(self, state, action, dn_doc=None):
        if not dn_doc:
            dn_doc = frappe.get_doc("Delivery Note", self.delivery_note)

        if dn_doc.form_sales_invoice:
            url = get_url_to_form("sales Ivoice", dn_doc.form_sales_invoice)
            frappe.throw(
                "Cannot create medicaton change request for items paid in cash<br>\
                refer sales invoice: <a href='{0}'>{1}</a>".format(
                    url, frappe.bold(dn_doc.form_sales_invoice)
                )
            )

        try:

            if dn_doc.workflow_state != state:
                apply_workflow(dn_doc, action)
                dn_doc.reload()

                if dn_doc.validate_workflow == "Changes Made":
                    frappe.msgprint(
                        _("Delivery Note " + self.delivery_note + " has been updated!"),
                        alert=True,
                    )

        except Exception:
            if state == "Changes Made":
                frappe.log_error(
                    frappe.get_traceback(),
                    str(
                        f"Apply workflow error for delivery note: {frappe.bold(dn_doc.name)}"
                    ),
                )
                frappe.throw(
                    f"Apply workflow error for delivery note: {frappe.bold(dn_doc.name)}"
                )

            else:
                frappe.log_error(frappe.get_traceback(), str(self.doctype))
                frappe.msgprint(
                    f"Apply workflow error for delivery note: {frappe.bold(dn_doc.name)}"
                )
                frappe.throw("Medication Change Request was not created, try again")


    def update_drug_prescription(self, patient_encounter_doc, dn_doc):
        for d in patient_encounter_doc.drug_prescription:
            for item in dn_doc.items:
                if d.name == item.reference_name:
                    frappe.db.set_value(
                        "Drug Prescription",
                        item.reference_name, {
                            "dn_detail": item.name,
                            "delivery_note": dn_doc.name,
                        },
                        update_modified=False
                    )


@frappe.whitelist()
def get_delivery_note(patient, patient_encounter):
    d_list = frappe.get_all(
        "Delivery Note",
        filters={"reference_name": patient_encounter, "docstatus": 0},
        fields=["name", "set_warehouse"],
    )
    if len(d_list) > 1:
        frappe.throw(
            "There is {0} delivery note of IPD and OPD warehouses, for patient: {1}, and encounter: {2}, \
            Please choose one delivery note between {3} and {4}".format(
                frappe.bold(len(d_list)),
                frappe.bold(patient),
                frappe.bold(patient_encounter),
                frappe.bold(d_list[0].name + ": warehouse: " + d_list[0].set_warehouse),
                frappe.bold(d_list[1].name + ": warehouse: " + d_list[1].set_warehouse),
            )
        )

    if len(d_list) == 1:
        return d_list[0].name
    if len(d_list) == 0:
        return ""


@frappe.whitelist()
def get_patient_encounter_name(delivery_note, sales_order):
    if delivery_note:
        return frappe.db.get_value("Delivery Note", delivery_note, "reference_name")

    elif sales_order:
        return frappe.db.get_value("Sales Order", sales_order, "patient_encounter")

    return ""


@frappe.whitelist()
def get_patient_encounter_doc(patient_encounter):
    doc = frappe.get_doc("Patient Encounter", patient_encounter)
    return doc


def get_insurance_details(self):
    insurance_subscription, insurance_company, mop = frappe.get_value(
        "Patient Appointment",
        self.appointment,
        ["insurance_subscription", "insurance_company", "mode_of_payment"],
    )
    return insurance_subscription, insurance_company, mop


def set_amount(self, row):
    item_code = frappe.get_cached_value("Medication", row.drug_code, "item")
    insurance_subscription, insurance_company, mop = get_insurance_details(self)

    # apply discount if it is available on Heathcare Insurance Company
    discount_percent = 0
    if insurance_company and "NHIF" not in insurance_company:
        discount_percent = get_discount_percent(insurance_company)

    if insurance_subscription and not row.prescribe:
        amount = get_item_rate(
            item_code, self.company, insurance_subscription, insurance_company
        )
        row.amount = amount - (amount * (discount_percent / 100))
        if discount_percent > 0:
            row.hms_tz_is_discount_applied = 1
            row.hms_tz_is_discount_percent = discount_percent

    elif mop:
        if not row.prescribe:
            row.prescribe = 1
        row.amount = get_mop_amount(item_code, mop, self.company, self.patient)


@frappe.whitelist()
def validate_healthcare_service_unit(warehouse, item, method):
    if warehouse != get_warehouse_from_service_unit(item.healthcare_service_unit):
        msgThrow(
            _(
                "Please change healthcare service unit: {0}, for drug: {1} row: {2}\
                as it is of different warehouse".format(
                    frappe.bold(item.healthcare_service_unit),
                    frappe.bold(item.drug_code),
                    frappe.bold(item.idx),
                )
            ),
            method,
        )


@frappe.whitelist()
def get_items_on_change_of_delivery_note(name, encounter, delivery_note):
    doc = frappe.get_doc("Medication Change Request", name)

    if not doc or not encounter or not delivery_note:
        return

    patient_encounter_doc = get_patient_encounter_doc(encounter)
    delivery_note_doc = frappe.get_doc("Delivery Note", delivery_note)

    doc.drug_prescription = []
    doc.original_pharmacy_prescription = []
    for item_line in patient_encounter_doc.drug_prescription:
        if delivery_note_doc.set_warehouse != get_warehouse_from_service_unit(
            item_line.healthcare_service_unit
        ):
            continue

        doc.set_drugs(
            item_line,
            insurance_subscription=patient_encounter_doc.insurance_subscription,
            inpatient_record=patient_encounter_doc.inpatient_record,
        )

    doc.delivery_note = delivery_note
    doc.save(ignore_permissions=True)
    doc.reload()
    return doc


@frappe.whitelist()
def get_items_on_change_of_sales_order(name, encounter, sales_order):
    doc = frappe.get_doc("Medication Change Request", name)

    if not doc or not encounter or not sales_order:
        return

    patient_encounter_doc = get_patient_encounter_doc(encounter)

    doc.drug_prescription = []
    doc.original_pharmacy_prescription = []
    for item_line in patient_encounter_doc.drug_prescription:
        doc.set_drugs(item_line)

    doc.sales_order = sales_order
    doc.save(ignore_permissions=True)
    doc.reload()
    return doc


def get_fields_to_clear():
    return ["name", "owner", "creation", "modified", "modified_by", "docstatus"]


def set_original_items(name, item):
    new_row = item.as_dict()
    for fieldname in get_fields_to_clear():
        new_row[fieldname] = None

    new_row.update(
        {
            "parent": name,
            "parentfield": "hms_tz_original_items",
            "parenttype": "Delivery Note",
            "doctype": "Original Delivery Note Item",
        }
    )

    return new_row


@frappe.whitelist()
def create_medication_change_request_from_dn(doctype, name):
    source_doc = frappe.get_doc(doctype, name)

    if source_doc.form_sales_invoice:
        url = get_url_to_form("sales Ivoice", source_doc.form_sales_invoice)
        frappe.throw(
            "Cannot create medicaton change request for items paid in cash,<br>\
            please refer sales invoice: <a href='{0}'>{1}</a>".format(
                url, frappe.bold(source_doc.form_sales_invoice)
            )
        )

    if not source_doc.hms_tz_comment:
        frappe.throw(
            "<b>No comment found on the delivery note, Please keep a comment and save the delivery note, before creating med change request</b>"
        )

    doc = frappe.new_doc("Medication Change Request")
    doc.patient = source_doc.patient
    doc.patient_name = source_doc.patient_name
    doc.appointment = source_doc.hms_tz_appointment_no
    doc.company = source_doc.company
    doc.patient_encounter = source_doc.reference_name
    doc.delivery_note = source_doc.name
    doc.healthcare_practitioner = source_doc.healthcare_practitioner
    doc.hms_tz_comment = source_doc.hms_tz_comment

    validate_nhif_patient_claim_status(
        "Medication Change Request", doc.company, doc.appointment
    )

    doc.save(ignore_permissions=True)
    url = get_url_to_form(doc.doctype, doc.name)
    frappe.msgprint(
        f"Draft Medication Change Request: <a href='{url}'>{frappe.bold(doc.name)}</a> is created"
    )
    return doc.name


@frappe.whitelist()
def create_medication_change_request_from_so(doctype, name):
    source_doc = frappe.get_doc(doctype, name)

    if not source_doc.med_change_request_comment:
        frappe.throw(
            "<b>No comment found on the sales order,\
                Please keep a comment and save the sales order, before creating med change request</b>"
        )

    appointment, practitioner = frappe.db.get_value(
        "Patient Encounter",
        source_doc.patient_encounter,
        ["appointment", "practitioner"],
    )
    doc = frappe.new_doc("Medication Change Request")
    doc.patient = source_doc.patient
    doc.patient_name = source_doc.patient_name
    doc.appointment = appointment
    doc.company = source_doc.company
    doc.patient_encounter = source_doc.patient_encounter
    doc.sales_order = source_doc.name
    doc.healthcare_practitioner = practitioner
    doc.hms_tz_comment = source_doc.med_change_request_comment

    doc.save(ignore_permissions=True)
    url = get_url_to_form(doc.doctype, doc.name)
    frappe.msgprint(
        f"Draft Medication Change Request: <a href='{url}'>{frappe.bold(doc.name)}</a> is created"
    )
    return doc.name
